"""
Nado Exchange Wrapper for multi-perp-dex
Nado Protocol (INK L2 기반 오더북 DEX)
nado_protocol SDK 사용
"""
from mpdex.base import MultiPerpDex, MultiPerpDexMixin
from typing import Optional, Dict, Any
import time


# Product ID 매핑
NADO_PRODUCT_IDS = {
    "BTC": 2,
    "ETH": 4,
    "SOL": 8,
}

# 사이즈 단위 (x18)
SIZE_INCREMENTS = {
    2: 50_000_000_000_000,       # BTC: 0.00005
    4: 1_000_000_000_000_000,    # ETH: 0.001
    8: 100_000_000_000_000_000,  # SOL: 0.1
}
DEFAULT_SIZE_INCREMENT = 50_000_000_000_000


class NadoExchange(MultiPerpDexMixin, MultiPerpDex):
    def __init__(
        self,
        private_key: str = None,
        use_mainnet: bool = True,
        slippage: float = 0.003,
    ):
        super().__init__()
        self.private_key = private_key
        self.use_mainnet = use_mainnet
        self.slippage = slippage
        self.client = None
        self.sub_hex = None
        self._size_cache = dict(SIZE_INCREMENTS)

    async def init(self):
        """nado_protocol 클라이언트 초기화"""
        from nado_protocol.client import NadoClientMode, create_nado_client
        from nado_protocol.utils.bytes32 import subaccount_to_hex
        from nado_protocol.utils.subaccount import SubaccountParams

        mode = NadoClientMode.MAINNET if self.use_mainnet else NadoClientMode.TESTNET
        self.client = create_nado_client(mode, self.private_key)

        owner = self.client.context.signer.address
        sa = SubaccountParams(subaccount_owner=owner, subaccount_name="default")
        self.sub_hex = subaccount_to_hex(sa)
        return self

    def _get_product_id(self, symbol: str) -> int:
        """심볼 → product_id 변환"""
        sym = symbol.upper().replace("-PERP", "").replace("_PERP", "").replace("/USD", "").replace("USD", "")
        if sym in NADO_PRODUCT_IDS:
            return NADO_PRODUCT_IDS[sym]
        raise ValueError(f"Nado: 지원하지 않는 심볼: {symbol}")

    def _normalize_size(self, product_id: int, size_x18: int) -> int:
        inc = self._size_cache.get(product_id, DEFAULT_SIZE_INCREMENT)
        return max(0, (int(size_x18) // inc) * inc)

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """오라클 가격 조회"""
        try:
            pid = self._get_product_id(symbol)
            pxs = self.client.context.indexer_client.get_oracle_prices([pid])
            price = int(pxs.prices[0].oracle_price_x18) / 1e18
            return price
        except Exception:
            return None

    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        """시장가 주문"""
        from nado_protocol.engine_client.types.execute import MarketOrderParams, PlaceMarketOrderParams

        pid = self._get_product_id(symbol)
        # amount는 일반 단위 (예: 0.001 BTC), x18로 변환
        size_x18 = int(amount * 1e18)
        size_x18 = self._normalize_size(pid, size_x18)

        if size_x18 <= 0:
            return None

        # side: "buy"/"sell" → long/short 방향 결정
        is_long = side.lower() in ("buy", "long")
        signed = size_x18 if is_long else -size_x18

        order = MarketOrderParams(sender=self.sub_hex, amount=str(signed))
        params = PlaceMarketOrderParams(product_id=pid, market_order=order, slippage=self.slippage)

        result = self.client.market.place_market_order(params)
        return result

    async def get_position(self, symbol: str) -> Optional[Dict]:
        """포지션 조회"""
        try:
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
                        "raw_data": {
                            "product_id": p_pid,
                            "notional_value": float(getattr(p, "notional_value", 0) or 0),
                            "margin_used": float(getattr(p, "margin_used", 0) or 0),
                        }
                    }
            return None
        except Exception:
            return None

    async def close_position(self, symbol, position=None, *, is_reduce_only=True):
        """포지션 청산"""
        if position is None:
            position = await self.get_position(symbol)
        if not position or float(position.get("size", 0)) == 0:
            return None

        # 반대 방향으로 시장가 주문
        close_side = "sell" if position["side"] == "long" else "buy"
        return await self.create_order(symbol, close_side, float(position["size"]))

    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        """Nado는 unified cross-margin — 별도 레버리지 설정 불필요"""
        # Nado는 통합 마진 시스템, 레버리지는 포지션 크기로 자동 결정
        return True

    async def get_collateral(self):
        """잔고 조회"""
        try:
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
        """연결 정리"""
        self.client = None
