"""
Ethereal DEX wrapper — ethereal-sdk 기반
https://docs.ethereal.trade/
"""
from mpdex.base import MultiPerpDex, MultiPerpDexMixin
from typing import Optional, Dict, Any
import logging
import os
import json

logger = logging.getLogger(__name__)

# linked signer key 캐시 파일 경로
_SIGNER_CACHE = os.path.join(os.path.dirname(__file__), "..", "..", ".cache", "ethereal_signer.json")


class EtherealExchange(MultiPerpDexMixin, MultiPerpDex):
    def __init__(self, private_key: str):
        super().__init__()
        self.private_key = private_key
        self.client = None
        self._products_cache: Dict[str, Any] = {}   # ticker -> ProductDto
        self._subaccount_id = None
        self._sender = None
        self._signer_key = None  # linked signer private key

    async def init(self):
        from ethereal import AsyncRESTClient

        self.client = await AsyncRESTClient.create({
            "base_url": "https://api.ethereal.trade",
            "chain_config": {
                "rpc_url": "https://rpc.ethereal.trade",
                "private_key": self.private_key,
            }
        })

        # 프로덕트 캐시
        products = await self.client.list_products()
        for p in products:
            self._products_cache[p.ticker] = p

        # 서브계정 조회
        subs = await self.client.subaccounts()
        if subs:
            self._subaccount_id = subs[0].id
            self._sender = subs[0].account
        else:
            logger.warning("[ethereal] 서브계정 없음 — 디포짓 후 자동 생성됨")
            return self

        # linked signer 설정
        await self._ensure_linked_signer()

        return self

    async def _ensure_linked_signer(self):
        """linked signer가 없으면 생성, 캐시에서 로드"""
        from eth_account import Account
        import secrets

        # 1) 캐시에서 기존 signer key 로드 시도
        cached_key = self._load_signer_cache()
        if cached_key:
            # 이 signer가 아직 active인지 확인
            try:
                cached_acct = Account.from_key(cached_key)
                signers = await self.client.list_signers(subaccount_id=str(self._subaccount_id))
                for s in signers:
                    if s.signer.lower() == cached_acct.address.lower() and str(s.status).upper() in ('ACTIVE', 'STATUS2.ACTIVE'):
                        self._signer_key = cached_key
                        logger.info(f"[ethereal] cached linked signer 사용: {cached_acct.address[:10]}...")
                        return
            except Exception:
                pass

        # 2) 기존 active signer가 있는지 확인 (웹에서 만든 것)
        try:
            signers = await self.client.list_signers(subaccount_id=str(self._subaccount_id))
            for s in signers:
                if str(s.status).upper() in ('ACTIVE', 'STATUS2.ACTIVE'):
                    logger.info(f"[ethereal] 기존 linked signer 발견: {s.signer[:10]}... (키 없음 — EOA 직접 서명)")
                    return  # signer key가 없으니 EOA로 직접 서명
        except Exception:
            pass

        # 3) 새 signer 생성 및 등록
        try:
            signer_key = '0x' + secrets.token_hex(32)
            signer_acct = Account.from_key(signer_key)

            result = await self.client.link_signer(
                signer=signer_acct.address,
                subaccount_id=self._subaccount_id,
                sender=self._sender,
                signer_private_key=signer_key,
            )
            logger.info(f"[ethereal] 새 linked signer 등록: {signer_acct.address[:10]}... status={result.status}")

            self._signer_key = signer_key
            self._save_signer_cache(signer_key)

            # PENDING → ACTIVE 대기 (최대 10초)
            import asyncio
            for _ in range(10):
                await asyncio.sleep(1)
                signers = await self.client.list_signers(subaccount_id=str(self._subaccount_id))
                for s in signers:
                    if s.signer.lower() == signer_acct.address.lower():
                        if str(s.status).upper() in ('ACTIVE', 'STATUS2.ACTIVE'):
                            logger.info(f"[ethereal] linked signer ACTIVE")
                            return
            logger.warning("[ethereal] linked signer 아직 PENDING — 주문 시 재시도")
        except Exception as e:
            logger.warning(f"[ethereal] linked signer 등록 실패: {e} — EOA 직접 서명 사용")

    def _load_signer_cache(self) -> Optional[str]:
        try:
            cache_path = os.path.normpath(_SIGNER_CACHE)
            if os.path.exists(cache_path):
                with open(cache_path, 'r') as f:
                    data = json.load(f)
                return data.get("signer_private_key")
        except Exception:
            pass
        return None

    def _save_signer_cache(self, signer_key: str):
        try:
            cache_path = os.path.normpath(_SIGNER_CACHE)
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, 'w') as f:
                json.dump({"signer_private_key": signer_key}, f)
        except Exception as e:
            logger.warning(f"[ethereal] signer 캐시 저장 실패: {e}")

    def _ticker(self, symbol: str) -> str:
        """BTC -> BTCUSD, ETH -> ETHUSD 등"""
        sym = symbol.upper().replace("-USD", "").replace("USD", "").replace("-PERP", "")
        return f"{sym}USD"

    def _product_id(self, symbol: str):
        ticker = self._ticker(symbol)
        p = self._products_cache.get(ticker)
        if not p:
            raise ValueError(f"Ethereal: 지원하지 않는 심볼: {symbol}")
        return p.id

    def _snap_to_lot_size(self, symbol: str, amount: float) -> float:
        """수량을 lotSize 배수로 스냅"""
        ticker = self._ticker(symbol)
        p = self._products_cache.get(ticker)
        if p and hasattr(p, 'lot_size'):
            lot = float(p.lot_size)
            if lot > 0:
                return round(int(amount / lot) * lot, 10)
        if p and hasattr(p, 'lotSize'):
            lot = float(p.lotSize)
            if lot > 0:
                return round(int(amount / lot) * lot, 10)
        # 기본 lotSize 0.0001
        return round(int(amount / 0.0001) * 0.0001, 10)

    def _snap_to_tick_size(self, symbol: str, price: float) -> float:
        """가격을 tickSize 배수로 스냅"""
        ticker = self._ticker(symbol)
        p = self._products_cache.get(ticker)
        tick = None
        for attr in ('tick_size', 'tickSize'):
            if p and hasattr(p, attr):
                tick = float(getattr(p, attr))
                if tick > 0:
                    break
        if not tick or tick <= 0:
            tick = 0.1  # Ethereal 기본 tickSize
        return round(round(price / tick) * tick, 10)

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        try:
            pid = self._product_id(symbol)
            prices = await self.client.list_market_prices(product_ids=[str(pid)])
            if prices:
                return float(prices[0].oracle_price)
        except Exception as e:
            logger.error(f"[ethereal] get_mark_price 실패: {e}")
        return None

    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        try:
            ticker = self._ticker(symbol)
            is_reduce_only = kwargs.get('is_reduce_only', False)

            # lotSize 배수로 스냅
            snapped_amount = self._snap_to_lot_size(symbol, float(amount))
            if snapped_amount <= 0:
                logger.warning(f"[ethereal] 수량 {amount} → lotSize 스냅 후 0, 스킵")
                return None

            # side: 'buy'/'long' -> 0, 'sell'/'short' -> 1
            side_int = 0 if side.lower() in ('buy', 'long') else 1

            order_params = {
                "order_type": "MARKET" if order_type == 'market' else "LIMIT",
                "quantity": snapped_amount,
                "side": side_int,
                "ticker": ticker,
                "reduce_only": is_reduce_only,
            }

            if order_type != 'market' and price is not None:
                order_params["price"] = self._snap_to_tick_size(symbol, float(price))

            result = await self._submit_order_with_retry(order_params)
            return result
        except Exception as e:
            logger.error(f"[ethereal] create_order 실패: {e}")
            raise

    async def _submit_order_with_retry(self, order_params: dict):
        """주문 제출 — 401 시 signer 재생성 후 1회 재시도"""
        for attempt in range(2):
            try:
                if self._signer_key:
                    from eth_account import Account
                    signer_acct = Account.from_key(self._signer_key)
                    order_params["sender"] = signer_acct.address

                    order = await self.client.create_order(**order_params, sign=False, submit=False)
                    signed = await self.client.sign_order(order, private_key=self._signer_key)
                    return await self.client.submit_order(signed)
                else:
                    return await self.client.create_order(**order_params)
            except Exception as e:
                err_str = str(e)
                if "401" in err_str or "Unauthorized" in err_str:
                    if attempt == 0:
                        logger.warning("[ethereal] 401 — linked signer 재생성 시도")
                        self._signer_key = None
                        await self._ensure_linked_signer()
                        continue
                raise

    async def get_position(self, symbol: str) -> Optional[Dict]:
        try:
            if not self._subaccount_id:
                return None

            pid = self._product_id(symbol)
            positions = await self.client.list_positions(
                subaccount_id=str(self._subaccount_id),
                product_ids=[str(pid)],
                open=True,
            )

            for pos in positions:
                size = float(pos.quantity) if hasattr(pos, 'quantity') else 0
                if size == 0:
                    size = float(pos.size) if hasattr(pos, 'size') else 0
                if size == 0:
                    continue

                side_val = getattr(pos, 'side', None)
                if side_val == 0 or str(side_val).upper() == 'BUY':
                    side_str = 'long'
                elif side_val == 1 or str(side_val).upper() == 'SELL':
                    side_str = 'short'
                else:
                    side_str = 'long' if size > 0 else 'short'

                entry_price = float(pos.average_entry_price) if hasattr(pos, 'average_entry_price') else 0
                unrealized_pnl = float(pos.unrealized_pnl) if hasattr(pos, 'unrealized_pnl') else 0
                liq_price = float(pos.liquidation_price) if hasattr(pos, 'liquidation_price') and pos.liquidation_price else None

                return {
                    "side": side_str,
                    "size": abs(size),
                    "entry_price": entry_price,
                    "unrealized_pnl": unrealized_pnl,
                    "leverage_type": "cross",
                    "leverage_value": None,
                    "liquidation_price": liq_price,
                }

            return None
        except Exception as e:
            logger.error(f"[ethereal] get_position 실패: {e}")
            return None

    async def get_collateral(self) -> Dict:
        try:
            if not self._subaccount_id:
                return {"total_collateral": 0, "available_collateral": 0}

            balances = await self.client.get_subaccount_balances(
                subaccount_id=str(self._subaccount_id)
            )

            total = 0
            available = 0
            for b in balances:
                total += float(b.amount) if hasattr(b, 'amount') else 0
                available += float(b.available) if hasattr(b, 'available') else 0

            return {
                "total_collateral": total,
                "available_collateral": available,
            }
        except Exception as e:
            logger.error(f"[ethereal] get_collateral 실패: {e}")
            return {"total_collateral": 0, "available_collateral": 0}

    async def get_open_orders(self, symbol) -> list:
        try:
            if not self._subaccount_id:
                return []

            pid = self._product_id(symbol)
            orders = await self.client.list_orders(
                subaccount_id=str(self._subaccount_id),
                product_ids=[str(pid)],
                is_working=True,
            )
            result = []
            for o in orders:
                result.append({
                    "id": str(o.id),
                    "symbol": symbol,
                    "side": "buy" if o.side == 0 else "sell",
                    "size": float(o.quantity) if hasattr(o, 'quantity') else 0,
                    "price": float(o.price) if hasattr(o, 'price') else None,
                })
            return result
        except Exception as e:
            logger.error(f"[ethereal] get_open_orders 실패: {e}")
            return []

    async def cancel_orders(self, symbol, open_orders=None):
        try:
            if open_orders is None:
                open_orders = await self.get_open_orders(symbol)
            if not open_orders:
                return []

            from uuid import UUID
            order_ids = [UUID(o["id"]) for o in open_orders]
            result = await self.client.cancel_orders(order_ids=order_ids)
            return result
        except Exception as e:
            logger.error(f"[ethereal] cancel_orders 실패: {e}")
            return []

    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        # Ethereal은 주문 시 레버리지가 마진으로 결정됨 (별도 API 없음)
        return {
            "symbol": symbol,
            "leverage": leverage,
            "margin_mode": margin_mode or "cross",
            "status": "ok",
        }

    async def get_leverage_info(self, symbol):
        ticker = self._ticker(symbol)
        p = self._products_cache.get(ticker)
        max_lev = int(p.max_leverage) if p and hasattr(p, 'max_leverage') else 20
        return {
            "symbol": symbol,
            "leverage": None,
            "margin_mode": "cross",
            "status": "ok",
            "max_leverage": max_lev,
            "available_margin_modes": ["cross"],
        }

    async def get_available_symbols(self):
        return {
            "perp": [f"{t.replace('USD', '')}-USD" for t in self._products_cache.keys()],
        }

    async def close(self):
        if self.client:
            await self.client.close()
            self.client = None
