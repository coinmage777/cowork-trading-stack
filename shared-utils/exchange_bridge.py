"""
Exchange Bridge (자식 프로세스)
===============================
별도 venv에서 실행되어 거래소 API를 호출하는 브릿지 프로세스.

메인 프로세스와 stdin/stdout으로 JSON-RPC 통신:
  → stdin: {"id": 1, "method": "get_mark_price", "params": {"symbol": "BTC"}}
  ← stdout: {"id": 1, "result": 83000.5}
  ← stdout: {"id": 1, "error": "some error message"}

사용법 (nado_venv에서):
  nado_venv/bin/python -m strategies.exchange_bridge --exchange nado --config config.yaml --account nado
"""

import asyncio
import json
import sys
import logging
import argparse
from pathlib import Path

# 로깅은 stderr로 (stdout은 JSON-RPC 전용)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BRIDGE-%(name)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


class NadoBridge:
    """Nado 거래소 브릿지"""

    def __init__(self, config: dict, account_name: str):
        self.config = config
        self.account_name = account_name
        self.client = None
        self.sub_hex = None
        self._size_cache = {}

    async def init(self):
        """nado_protocol 클라이언트 초기화"""
        from nado_protocol.client import NadoClientMode, create_nado_client
        from nado_protocol.utils.bytes32 import subaccount_to_hex
        from nado_protocol.utils.subaccount import SubaccountParams

        keys = self.config.get("exchanges", {}).get(self.account_name, {}).get("keys", {})
        private_key = keys.get("private_key", "")
        use_mainnet = keys.get("use_mainnet", True)

        mode = NadoClientMode.MAINNET if use_mainnet else NadoClientMode.TESTNET
        self.client = create_nado_client(mode, private_key)

        owner = self.client.context.signer.address
        sa = SubaccountParams(subaccount_owner=owner, subaccount_name="default")
        self.sub_hex = subaccount_to_hex(sa)

        # 사이즈 단위 캐시
        self._size_cache = {
            2: 50_000_000_000_000,       # BTC: 0.00005
            4: 1_000_000_000_000_000,    # ETH: 0.001
            8: 100_000_000_000_000_000,  # SOL: 0.1
        }

        logger.info(f"Nado 클라이언트 초기화 완료 (owner: {owner})")
        return self

    # Product ID 매핑
    PRODUCT_IDS = {"BTC": 2, "ETH": 4, "SOL": 8}

    def _get_product_id(self, symbol: str) -> int:
        sym = symbol.upper().replace("-PERP", "").replace("_PERP", "").replace("/USD", "").replace("USD", "")
        if sym in self.PRODUCT_IDS:
            return self.PRODUCT_IDS[sym]
        raise ValueError(f"Nado: 지원하지 않는 심볼: {symbol}")

    def _normalize_size(self, product_id: int, size_x18: int) -> int:
        inc = self._size_cache.get(product_id, 50_000_000_000_000)
        return max(0, (int(size_x18) // inc) * inc)

    async def get_mark_price(self, symbol: str):
        try:
            pid = self._get_product_id(symbol)
            pxs = self.client.context.indexer_client.get_oracle_prices([pid])
            price = int(pxs.prices[0].oracle_price_x18) / 1e18
            return price
        except Exception as e:
            logger.error(f"get_mark_price 실패: {e}")
            return None

    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        try:
            from nado_protocol.engine_client.types.execute import MarketOrderParams, PlaceMarketOrderParams

            pid = self._get_product_id(symbol)
            size_x18 = int(float(amount) * 1e18)
            size_x18 = self._normalize_size(pid, size_x18)

            if size_x18 <= 0:
                return None

            is_long = str(side).lower() in ("buy", "long")
            signed = size_x18 if is_long else -size_x18

            order = MarketOrderParams(sender=self.sub_hex, amount=str(signed))
            params = PlaceMarketOrderParams(product_id=pid, market_order=order, slippage=0.003)

            result = self.client.market.place_market_order(params)
            return str(result)
        except Exception as e:
            logger.error(f"create_order 실패: {e}")
            raise

    async def get_position(self, symbol: str):
        try:
            import time
            from nado_protocol.utils.margin_manager import MarginManager

            pid = self._get_product_id(symbol)
            mgr = MarginManager.from_client(
                self.client,
                include_indexer_events=True,
                snapshot_timestamp=int(time.time()),
            )
            summary = mgr.calculate_account_summary()

            positions = (getattr(summary, "cross_positions", None) or []) + \
                        (getattr(summary, "isolated_positions", None) or [])

            for p in positions:
                p_pid = getattr(p, "product_id", None)
                if p_pid == pid:
                    size = float(getattr(p, "position_size", 0) or 0)
                    if size == 0:
                        continue
                    return {
                        "symbol": symbol,
                        "side": "long" if size > 0 else "short",
                        "size": abs(size),
                        "entry_price": float(getattr(p, "avg_entry_price", 0) or 0),
                        "unrealized_pnl": float(getattr(p, "est_pnl", 0) or 0),
                        "liquidation_price": float(getattr(p, "est_liq_price", 0) or 0) if getattr(p, "est_liq_price", None) else None,
                    }
            return None
        except Exception as e:
            logger.error(f"get_position 실패: {e}")
            return None

    async def close_position(self, symbol, position=None, **kwargs):
        if position is None:
            position = await self.get_position(symbol)
        if not position or float(position.get("size", 0)) == 0:
            return None
        close_side = "sell" if position["side"] == "long" else "buy"
        return await self.create_order(symbol, close_side, float(position["size"]))

    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        # Nado는 unified cross-margin
        return True

    async def get_collateral(self):
        try:
            import time
            from nado_protocol.utils.margin_manager import MarginManager
            mgr = MarginManager.from_client(
                self.client,
                include_indexer_events=True,
                snapshot_timestamp=int(time.time()),
            )
            summary = mgr.calculate_account_summary()
            return float(getattr(summary, "portfolio_value", 0))
        except Exception:
            return None

    async def get_open_orders(self, symbol):
        return []

    async def cancel_orders(self, symbol):
        return None

    async def close(self):
        self.client = None


class BulkBridge:
    """Bulk Trade 거래소 브릿지 (bulk-keychain + REST API)"""

    API_BASE = "https://exchange-api.bulk.trade/api/v1"

    def __init__(self, config: dict, account_name: str):
        self.config = config
        self.account_name = account_name
        self.signer = None
        self.pubkey = None
        self._session = None
        self._markets = {}  # symbol -> market info

    async def init(self):
        """bulk-keychain Signer 초기화 + 마켓 정보 로드"""
        from bulk_keychain import Keypair, Signer
        import aiohttp

        keys = self.config.get("exchanges", {}).get(self.account_name, {}).get("keys", {})
        private_key = keys.get("private_key", "")

        # 키 형식 감지: 0x로 시작하면 EVM hex, 아니면 base58
        if private_key.startswith("0x"):
            kp = Keypair.from_bytes(bytes.fromhex(private_key[2:]))
        else:
            kp = Keypair.from_base58(private_key)
        self.signer = Signer(kp)
        self.signer.set_compute_order_id(True)
        self.pubkey = kp.pubkey

        self._session = aiohttp.ClientSession()

        # 마켓 정보 로드
        await self._load_markets()

        logger.info(f"Bulk Trade 초기화 완료 (pubkey: {self.pubkey[:16]}...)")
        return self

    async def _api_get(self, endpoint: str, params: dict = None):
        import aiohttp
        url = f"{self.API_BASE}/{endpoint}"
        try:
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"[bulk] GET {endpoint} → {resp.status}: {text[:200]}")
                    return None
                return await resp.json()
        except Exception as e:
            logger.error(f"[bulk] GET {endpoint} 에러: {e}")
            return None

    async def _api_post(self, endpoint: str, data: dict):
        import aiohttp
        url = f"{self.API_BASE}/{endpoint}"
        try:
            async with self._session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning(f"[bulk] POST {endpoint} → {resp.status}: {text[:300]}")
                    return None
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(f"[bulk] POST {endpoint} → JSON 파싱 실패: {text[:200]}")
                    return None
        except Exception as e:
            logger.error(f"[bulk] POST {endpoint} 에러: {e}")
            return None

    async def _load_markets(self):
        """GET /exchangeInfo → 마켓 메타데이터 캐시"""
        data = await self._api_get("exchangeInfo")
        if not data:
            logger.warning("[bulk] exchangeInfo 로드 실패")
            return
        # exchangeInfo는 배열 형태로 마켓 정보 반환
        markets = data if isinstance(data, list) else data.get("markets", data.get("symbols", []))
        for m in markets:
            sym = m.get("symbol", m.get("s", ""))
            if sym:
                self._markets[sym] = m
        logger.info(f"[bulk] 마켓 {len(self._markets)}개 로드: {list(self._markets.keys())[:10]}")

    def _normalize_symbol(self, symbol: str) -> str:
        """BTC → BTC-USD"""
        sym = symbol.upper().replace("-PERP", "").replace("_PERP", "").replace("/USD", "").replace("USD", "")
        return f"{sym}-USD"

    async def get_mark_price(self, symbol: str):
        try:
            sym = self._normalize_symbol(symbol)
            data = await self._api_get(f"ticker/{sym}")
            if data:
                # markPrice or markPx
                price = data.get("markPrice") or data.get("markPx") or data.get("lastPrice") or data.get("c")
                if price is not None:
                    return float(price)
            return None
        except Exception as e:
            logger.error(f"[bulk] get_mark_price 실패: {e}")
            return None

    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        try:
            sym = self._normalize_symbol(symbol)
            is_buy = str(side).lower() in ("buy", "long")
            is_reduce_only = kwargs.get("is_reduce_only", False)
            tif = kwargs.get("tif", None)

            size = float(amount)
            if size <= 0:
                return None

            if order_type == 'market' or price is None:
                # 시장가: IOC + 슬리피지 가격
                mark = await self.get_mark_price(symbol)
                if not mark:
                    raise RuntimeError(f"[bulk] 마크 가격 조회 실패: {symbol}")
                slippage = 0.01  # 1%
                aggressive_price = mark * (1 + slippage) if is_buy else mark * (1 - slippage)
                order_data = {
                    "type": "order",
                    "symbol": sym,
                    "is_buy": is_buy,
                    "price": round(aggressive_price, 2),
                    "size": size,
                    "reduce_only": is_reduce_only,
                    "order_type": {"type": "limit", "tif": "IOC"},
                }
            else:
                # 지정가
                effective_tif = "ALO" if tif and tif.upper() in ("ALO", "POST_ONLY") else "GTC"
                order_data = {
                    "type": "order",
                    "symbol": sym,
                    "is_buy": is_buy,
                    "price": float(price),
                    "size": size,
                    "reduce_only": is_reduce_only,
                    "order_type": {"type": "limit", "tif": effective_tif},
                }

            signed = self.signer.sign(order_data)
            result = await self._api_post("order", signed)

            if result:
                statuses = result.get("response", {}).get("data", {}).get("statuses", []) if isinstance(result.get("response"), dict) else []
                if not statuses:
                    # 직접 statuses 확인
                    statuses = result.get("statuses", [])
                return {"result": result, "order_id": signed.get("order_id"), "statuses": statuses}
            return result
        except Exception as e:
            logger.error(f"[bulk] create_order 실패: {e}")
            raise

    async def get_position(self, symbol: str):
        try:
            sym = self._normalize_symbol(symbol)
            data = await self._api_post("account", {"type": "fullAccount", "user": self.pubkey})
            if not data:
                return None

            # fullAccount → positions 배열에서 해당 심볼 찾기
            account_data = data[0] if isinstance(data, list) else data
            if isinstance(account_data, dict) and "fullAccount" in account_data:
                account_data = account_data["fullAccount"]

            positions = account_data.get("positions", [])
            for p in positions:
                p_sym = p.get("symbol", p.get("s", ""))
                if p_sym.upper() == sym.upper():
                    size = float(p.get("size", p.get("sz", 0)))
                    if size == 0:
                        continue
                    entry_price = float(p.get("price", p.get("entryPrice", p.get("avgEntryPrice", 0))))
                    unrealized_pnl = float(p.get("unrealizedPnl", p.get("pnl", 0)))
                    liq_price = p.get("liquidationPrice", p.get("liquidation_price"))
                    liq_val = float(liq_price) if liq_price and float(liq_price) > 0 else None

                    return {
                        "symbol": symbol,
                        "side": "long" if size > 0 else "short",
                        "size": abs(size),
                        "entry_price": entry_price,
                        "unrealized_pnl": unrealized_pnl,
                        "liquidation_price": liq_val,
                    }
            return None
        except Exception as e:
            logger.error(f"[bulk] get_position 실패: {e}")
            return None

    async def close_position(self, symbol, position=None, **kwargs):
        if position is None:
            position = await self.get_position(symbol)
        if not position or float(position.get("size", 0)) == 0:
            return None
        close_side = "sell" if position["side"] == "long" else "buy"
        return await self.create_order(
            symbol, close_side, float(position["size"]),
            order_type='market', is_reduce_only=True,
        )

    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        """POST /order로 레버리지 설정 (updateUserSettings action)"""
        try:
            if leverage is None:
                return True
            sym = self._normalize_symbol(symbol)
            signed = self.signer.sign_user_settings([(sym, float(leverage))])
            # keychain이 m을 array로 보내는데 API는 map을 기대 → 변환
            for action in signed.get("actions", []):
                if "updateUserSettings" in action:
                    m_val = action["updateUserSettings"].get("m", [])
                    if isinstance(m_val, list):
                        action["updateUserSettings"]["m"] = {k: v for k, v in m_val}
            result = await self._api_post("order", signed)
            return result
        except Exception as e:
            logger.error(f"[bulk] update_leverage 실패: {e}")
            return None

    async def get_collateral(self):
        try:
            data = await self._api_post("account", {"type": "fullAccount", "user": self.pubkey})
            if not data:
                return None

            account_data = data[0] if isinstance(data, list) else data
            if isinstance(account_data, dict) and "fullAccount" in account_data:
                account_data = account_data["fullAccount"]

            margin = account_data.get("margin", {})
            total = float(margin.get("totalBalance", margin.get("equity", margin.get("accountValue", 0))))
            available = float(margin.get("availableBalance", total))

            return total if total else 0
        except Exception as e:
            logger.error(f"[bulk] get_collateral 실패: {e}")
            return None

    async def get_open_orders(self, symbol):
        try:
            data = await self._api_post("account", {"type": "openOrders", "user": self.pubkey})
            if not data:
                return []

            sym = self._normalize_symbol(symbol)
            orders = []
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and "openOrder" in item:
                    o = item["openOrder"]
                elif isinstance(item, dict):
                    o = item
                else:
                    continue

                o_sym = o.get("symbol", o.get("s", ""))
                if o_sym.upper() != sym.upper():
                    continue

                orders.append({
                    "id": o.get("orderId", o.get("oid", "")),
                    "symbol": symbol,
                    "side": "buy" if o.get("isBuy", o.get("b")) else "sell",
                    "size": float(o.get("size", o.get("sz", 0))),
                    "price": float(o.get("price", o.get("px", 0))),
                })
            return orders
        except Exception as e:
            logger.error(f"[bulk] get_open_orders 실패: {e}")
            return []

    async def cancel_orders(self, symbol):
        try:
            sym = self._normalize_symbol(symbol)
            signed = self.signer.sign({"type": "cancel_all", "symbols": [sym]})
            result = await self._api_post("order", signed)
            return result
        except Exception as e:
            logger.error(f"[bulk] cancel_orders 실패: {e}")
            return None

    async def request_faucet(self):
        """테스트넷 파우셋 요청"""
        try:
            signed = self.signer.sign_faucet()
            result = await self._api_post("order", signed)
            logger.info(f"[bulk] 파우셋 결과: {result}")
            return result
        except Exception as e:
            logger.error(f"[bulk] 파우셋 요청 실패: {e}")
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None


class HotstuffBridge:
    """Hotstuff 거래소 브릿지 (hotstuff-python-sdk)"""

    def __init__(self, config: dict, account_name: str):
        self.config = config
        self.account_name = account_name
        self._exchange = None  # HotstuffExchange instance

    async def init(self):
        """HotstuffExchange 초기화"""
        # mpdex.exchanges.hotstuff를 직접 import
        from mpdex.exchanges.hotstuff import HotstuffExchange

        keys = self.config.get("exchanges", {}).get(self.account_name, {}).get("keys", {})
        private_key = keys.get("private_key", "")
        is_testnet = keys.get("is_testnet", False)

        self._exchange = HotstuffExchange(
            private_key=private_key,
            is_testnet=is_testnet,
        )
        await self._exchange.init()
        logger.info(f"Hotstuff 클라이언트 초기화 완료")
        return self

    async def get_mark_price(self, symbol: str):
        return await self._exchange.get_mark_price(symbol)

    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        result = await self._exchange.create_order(symbol, side, amount, price, order_type, **kwargs)
        return result

    async def get_position(self, symbol: str):
        return await self._exchange.get_position(symbol)

    async def close_position(self, symbol, position=None, **kwargs):
        return await self._exchange.close_position(symbol, position, **kwargs)

    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        return await self._exchange.update_leverage(symbol, leverage)

    async def get_collateral(self):
        coll = await self._exchange.get_collateral()
        if isinstance(coll, dict):
            return coll.get("total_collateral", 0)
        return coll

    async def get_open_orders(self, symbol):
        return await self._exchange.get_open_orders(symbol)

    async def cancel_orders(self, symbol):
        return await self._exchange.cancel_orders(symbol)

    async def close(self):
        await self._exchange.close()


# ============================================================
# JSON-RPC 메시지 루프
# ============================================================

async def handle_request(bridge, request: dict) -> dict:
    """JSON-RPC 요청 처리"""
    req_id = request.get("id", 0)
    method = request.get("method", "")
    params = request.get("params", {})

    try:
        if method == "init":
            await bridge.init()
            result = "ok"
        elif method == "get_mark_price":
            result = await bridge.get_mark_price(params.get("symbol", ""))
        elif method == "create_order":
            result = await bridge.create_order(
                symbol=params.get("symbol", ""),
                side=params.get("side", ""),
                amount=params.get("amount", 0),
                price=params.get("price"),
                order_type=params.get("order_type", "market"),
            )
        elif method == "get_position":
            result = await bridge.get_position(params.get("symbol", ""))
        elif method == "close_position":
            result = await bridge.close_position(
                symbol=params.get("symbol", ""),
                position=params.get("position"),
            )
        elif method == "update_leverage":
            result = await bridge.update_leverage(
                symbol=params.get("symbol", ""),
                leverage=params.get("leverage"),
                margin_mode=params.get("margin_mode"),
            )
        elif method == "get_collateral":
            result = await bridge.get_collateral()
        elif method == "get_balance":
            # 오토스케일러용 — get_collateral과 동일 (portfolio value)
            result = await bridge.get_collateral()
        elif method == "get_open_orders":
            result = await bridge.get_open_orders(params.get("symbol", ""))
        elif method == "cancel_orders":
            result = await bridge.cancel_orders(params.get("symbol", ""))
        elif method == "close":
            await bridge.close()
            result = "ok"
        elif method == "ping":
            result = "pong"
        else:
            return {"id": req_id, "error": f"Unknown method: {method}"}

        return {"id": req_id, "result": result}

    except Exception as e:
        logger.error(f"요청 처리 에러 [{method}]: {e}")
        return {"id": req_id, "error": str(e)}


async def message_loop(bridge):
    """stdin에서 JSON 메시지를 읽고 stdout으로 응답"""
    logger.info("브릿지 메시지 루프 시작")

    loop = asyncio.get_event_loop()

    # 윈도우 호환: 스레드에서 stdin 읽기 (ProactorEventLoop 파이프 문제 우회)
    import concurrent.futures
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def _read_stdin_line():
        """블로킹 stdin 읽기 (스레드에서 실행)"""
        try:
            return sys.stdin.buffer.readline()
        except Exception:
            return b""

    while True:
        try:
            line = await loop.run_in_executor(executor, _read_stdin_line)
            if not line:
                logger.info("stdin EOF — 브릿지 종료")
                break

            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue

            request = json.loads(line_str)
            response = await handle_request(bridge, request)

            # stdout으로 응답 (반드시 한 줄로)
            response_json = json.dumps(response, ensure_ascii=False, default=str)
            sys.stdout.write(response_json + "\n")
            sys.stdout.flush()

        except json.JSONDecodeError as e:
            logger.error(f"JSON 파싱 에러: {e}")
            err_resp = json.dumps({"id": 0, "error": f"JSON parse error: {e}"})
            sys.stdout.write(err_resp + "\n")
            sys.stdout.flush()
        except Exception as e:
            logger.error(f"메시지 루프 에러: {e}")
            break

    executor.shutdown(wait=False)
    # 정리
    await bridge.close()
    logger.info("브릿지 종료 완료")


def load_config(config_path: str) -> dict:
    import yaml
    from dotenv import load_dotenv
    load_dotenv()
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    # resolve ${ENV_VAR} references
    from .env_loader import resolve_env_vars
    return resolve_env_vars(config)


class ReyaBridge:
    """Reya Network 거래소 브릿지 (reya-python-sdk, Python 3.12+)"""

    def __init__(self, config: dict, account_name: str):
        self.config = config
        self.account_name = account_name
        self._client = None

    async def init(self):
        from sdk.reya_rest_api import ReyaTradingClient, TradingConfig
        keys = self.config.get("exchanges", {}).get(self.account_name, {}).get("keys", {})
        cfg = TradingConfig(
            chain_id=1729,
            api_url='https://api.reya.xyz/v2',
            owner_wallet_address=keys.get("owner_wallet_address", ""),
            private_key=keys.get("private_key", ""),
            account_id=int(keys.get("account_id", 0)),
        )
        self._client = ReyaTradingClient(cfg)
        await self._client.start()
        logger.info(f"Reya 클라이언트 초기화 완료 (account={keys.get('account_id')})")
        return self

    async def get_mark_price(self, symbol: str):
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get('https://api.reya.xyz/v2/markets/summary', timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
                    for m in data:
                        if m.get('symbol') == symbol:
                            return float(m.get('throttledPoolPrice', 0))
        except Exception as e:
            logger.debug(f"[reya] mark_price 조회 실패: {e}")
        return 0.0

    # qty_step_size per symbol
    STEP_SIZES = {"BTCRUSDPERP": 0.0001, "ETHRUSDPERP": 0.001, "SOLRUSDPERP": 0.01}

    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        from sdk.reya_rest_api.models.orders import LimitOrderParameters
        from sdk.open_api.models.time_in_force import TimeInForce
        import math
        is_buy = side.lower() == 'buy'
        reduce_only = kwargs.get('reduce_only', False)
        if price is None or order_type == 'market':
            mark = await self.get_mark_price(symbol)
            # 슬리피지: 일반 2%, reduce_only 청산은 5% (체결 보장 우선)
            slip = 0.05 if reduce_only else 0.02
            price = mark * (1 + slip) if is_buy else mark * (1 - slip)
        # stepSize 반올림
        step = self.STEP_SIZES.get(symbol, 0.001)
        amount = math.floor(float(amount) / step) * step
        if amount <= 0:
            return {"status": "error", "message": "amount too small"}
        # reduce_only: 실제 포지션 크기로 clamping (bot 메모리-거래소 불일치 방지)
        if reduce_only:
            actual = await self.get_position(symbol)
            if actual and float(actual.get("size", 0)) > 0:
                amount = min(amount, math.floor(float(actual["size"]) / step) * step)
                if amount <= 0:
                    return {"status": "error", "message": "no position to close"}
        # precision
        decimals = len(str(step).rstrip('0').split('.')[-1]) if '.' in str(step) else 0
        order = LimitOrderParameters(
            symbol=symbol,
            is_buy=is_buy,
            limit_px=str(round(price, 3)),
            qty=f"{amount:.{decimals}f}",
            time_in_force=TimeInForce.IOC,
            reduce_only=reduce_only,
        )
        result = await self._client.create_limit_order(order)
        return {"status": result.status.value if result.status else "unknown"}

    async def get_position(self, symbol: str):
        positions = await self._client.get_positions()
        for p in positions:
            if p.symbol == symbol:
                is_long = p.side.value == 'B'
                return {
                    "symbol": symbol,
                    "side": "long" if is_long else "short",
                    "size": abs(float(p.qty)),
                    "entry_price": float(p.avg_entry_price) if p.avg_entry_price else 0,
                    "unrealized_pnl": 0,
                }
        return None

    async def close_position(self, symbol, position=None, **kwargs):
        if position is None:
            position = await self.get_position(symbol)
        if not position or float(position.get("size", 0)) == 0:
            return None
        close_side = "sell" if position["side"] == "long" else "buy"
        return await self.create_order(symbol, close_side, position["size"],
                                       order_type="market", reduce_only=True)

    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        return True

    async def get_collateral(self):
        balances = await self._client.get_account_balances()
        if balances:
            return float(balances[0].real_balance)
        return 0

    async def get_open_orders(self, symbol):
        orders = await self._client.get_open_orders()
        return [o for o in orders if hasattr(o, 'symbol') and o.symbol == symbol]

    async def cancel_orders(self, symbol):
        try:
            await self._client.mass_cancel()
        except Exception:
            pass
        return []


class GrvtBridge:
    """GRVT 거래소 브릿지"""

    def __init__(self, config: dict, account_name: str):
        self.config = config
        self.account_name = account_name
        self._exchange = None

    async def init(self):
        from mpdex.exchanges.grvt import GrvtExchange
        keys = self.config.get("exchanges", {}).get(self.account_name, {}).get("keys", {})
        self._exchange = GrvtExchange(
            api_key=keys.get("api_key", ""),
            account_id=keys.get("account_id", ""),
            secret_key=keys.get("secret_key", ""),
            use_ws=False,  # bridge에서는 REST만 사용 (WS는 이벤트 루프 충돌)
        )
        await self._exchange.init()
        logger.info(f"GRVT 클라이언트 초기화 완료 (REST only)")
        return self

    async def get_mark_price(self, symbol: str):
        try:
            result = await self._exchange.get_mark_price(symbol)
            if isinstance(result, dict):
                return float(result.get("mark_price", result.get("price", 0)))
            return float(result) if result else 0.0
        except Exception as e:
            logger.warning(f"[grvt] get_mark_price 실패: {e}")
            return 0.0

    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        return await self._exchange.create_order(symbol, side, amount, price, order_type, **kwargs)

    async def get_position(self, symbol: str):
        return await self._exchange.get_position(symbol)

    async def close_position(self, symbol, position=None, **kwargs):
        return await self._exchange.close_position(symbol, position, **kwargs)

    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        return await self._exchange.update_leverage(symbol, leverage)

    async def get_collateral(self):
        coll = await self._exchange.get_collateral()
        if isinstance(coll, dict):
            return coll.get("total_collateral", 0)
        return coll

    async def get_open_orders(self, symbol):
        return await self._exchange.get_open_orders(symbol)

    async def cancel_orders(self, symbol):
        return await self._exchange.cancel_orders(symbol)


class LighterBridge:
    """Lighter 거래소 브릿지 (lighter-sdk)"""

    def __init__(self, config: dict, account_name: str):
        self.config = config
        self.account_name = account_name
        self._exchange = None

    async def init(self):
        from mpdex.exchanges.lighter import LighterExchange
        keys = self.config.get("exchanges", {}).get(self.account_name, {}).get("keys", {})
        self._exchange = LighterExchange(
            account_id=keys.get("account_id", ""),
            private_key=keys.get("private_key", ""),
            api_key_id=keys.get("api_key_id", ""),
            l1_address=keys.get("l1_address", ""),
        )
        await self._exchange.init()
        logger.info(f"Lighter 클라이언트 초기화 완료")
        return self

    async def get_mark_price(self, symbol: str):
        return await self._exchange.get_mark_price(symbol)

    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        return await self._exchange.create_order(symbol, side, amount, price, order_type, **kwargs)

    async def get_position(self, symbol: str):
        return await self._exchange.get_position(symbol)

    async def close_position(self, symbol, position=None, **kwargs):
        return await self._exchange.close_position(symbol, position, **kwargs)

    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        return await self._exchange.update_leverage(symbol, leverage)

    async def get_collateral(self):
        coll = await self._exchange.get_collateral()
        if isinstance(coll, dict):
            return coll.get("total_collateral", 0)
        return coll

    async def get_open_orders(self, symbol):
        return await self._exchange.get_open_orders(symbol)

    async def cancel_orders(self, symbol):
        return await self._exchange.cancel_orders(symbol)


class EdgeXBridge:
    """EdgeX StarkNet DEX — get_mark_price가 Decimal 반환하므로 float 캐스팅 필요"""
    """EdgeX 거래소 브릿지 (starkware/fastecdsa — Python 3.10 필수)"""

    def __init__(self, config: dict, account_name: str):
        self.config = config
        self.account_name = account_name
        self._exchange = None

    async def init(self):
        from mpdex.exchanges.edgex import EdgexExchange as EdgeXExchange
        keys = self.config.get("exchanges", {}).get(self.account_name, {}).get("keys", {})
        self._exchange = EdgeXExchange(
            account_id=keys.get("account_id", ""),
            private_key=keys.get("private_key", ""),
        )
        await self._exchange.init()
        logger.info(f"EdgeX 클라이언트 초기화 완료 (account={keys.get('account_id', '')})")
        return self

    async def get_mark_price(self, symbol: str):
        result = await self._exchange.get_mark_price(symbol)
        return float(result) if result else None

    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        return await self._exchange.create_order(symbol, side, amount, price, order_type, **kwargs)

    async def get_position(self, symbol: str):
        return await self._exchange.get_position(symbol)

    async def close_position(self, symbol, position=None, **kwargs):
        return await self._exchange.close_position(symbol, position, **kwargs)

    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        return await self._exchange.update_leverage(symbol, leverage)

    async def get_collateral(self):
        coll = await self._exchange.get_collateral()
        if isinstance(coll, dict):
            return coll.get("total_collateral", 0)
        return coll

    async def get_open_orders(self, symbol):
        return await self._exchange.get_open_orders(symbol)

    async def cancel_orders(self, symbol):
        return await self._exchange.cancel_orders(symbol)


def main():
    parser = argparse.ArgumentParser(description="Exchange Bridge Process")
    parser.add_argument("--exchange", required=True, help="거래소 이름 (e.g., nado)")
    parser.add_argument("--config", default="config.yaml", help="설정 파일 경로")
    parser.add_argument("--account", default=None, help="config.yaml의 거래소 키 이름 (기본값: --exchange와 동일)")
    args = parser.parse_args()

    account_name = args.account or args.exchange
    config = load_config(args.config)

    if args.exchange in ("nado", "nado_2"):
        bridge = NadoBridge(config, account_name)
    elif args.exchange == "hotstuff":
        bridge = HotstuffBridge(config, account_name)
    elif args.exchange == "bulk":
        bridge = BulkBridge(config, account_name)
    elif args.exchange == "lighter":
        bridge = LighterBridge(config, account_name)
    elif args.exchange == "grvt":
        bridge = GrvtBridge(config, account_name)
    elif args.exchange == "reya":
        bridge = ReyaBridge(config, account_name)
    elif args.exchange in ("edgex", "edgex_2"):
        bridge = EdgeXBridge(config, account_name)
    else:
        logger.error(f"지원하지 않는 거래소: {args.exchange}")
        sys.exit(1)

    asyncio.run(message_loop(bridge))


if __name__ == "__main__":
    main()
