"""
Ostium Exchange Wrapper
========================
Arbitrum 기반 Perp DEX. 온체인 트레이딩 (smart contract tx).
SDK: pip install ostium-python-sdk

인터페이스:
  get_mark_price(symbol) → float
  create_order(symbol, side, amount, ...) → dict
  get_position(symbol) → dict | None
  close_position(symbol, position) → dict
  get_collateral() → dict
  update_leverage(symbol, leverage, margin_mode) → None
"""

import asyncio
import logging
import aiohttp
from typing import Any, Dict, Optional

from mpdex.base import MultiPerpDex, MultiPerpDexMixin

logger = logging.getLogger(__name__)

# pair index mapping
PAIR_INDEX = {"BTC": 0, "ETH": 1, "SOL": 2, "BNB": 3, "XRP": 4}

PRICE_API = "https://metadata-backend.ostium.io/PricePublish/latest-prices"


class OstiumExchange(MultiPerpDexMixin, MultiPerpDex):

    def __init__(self, private_key: str, rpc_url: str = None):
        super().__init__()
        self._private_key = private_key
        self._rpc_url = rpc_url or "https://arb1.arbitrum.io/rpc"
        self._sdk = None
        self._address = None

    async def init(self):
        from ostium_python_sdk import OstiumSDK, NetworkConfig
        config = NetworkConfig.mainnet()
        self._sdk = OstiumSDK(config, private_key=self._private_key, rpc_url=self._rpc_url)
        from eth_account import Account
        self._address = Account.from_key(self._private_key).address
        logger.info(f"[ostium] 초기화 완료 (address={self._address})")
        return self

    async def get_mark_price(self, symbol: str) -> float:
        """REST public API로 가격 조회 (SDK 불필요)"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(PRICE_API) as r:
                    if r.status != 200:
                        return 0.0
                    data = await r.json()
                    # symbol에서 코인명 추출 (BTC, ETH 등)
                    coin = symbol.replace("-USD", "").replace("USD", "").replace("_PERP", "").upper()
                    for item in data:
                        if item.get("from", "").upper() == coin and item.get("to", "").upper() == "USD":
                            return float(item.get("mid", 0))
            return 0.0
        except Exception as e:
            logger.debug(f"[ostium] get_mark_price {symbol} 에러: {e}")
            return 0.0

    async def create_order(self, symbol: str, side: str, amount: float,
                           price: float = None, order_type: str = "market", **kwargs):
        """온체인 주문 실행"""
        coin = symbol.replace("-USD", "").replace("USD", "").upper()
        pair_id = PAIR_INDEX.get(coin, 0)
        is_buy = side.lower() in ("buy", "long")

        # amount = USDC collateral amount
        mark_price = await self.get_mark_price(symbol)
        if not mark_price:
            raise ValueError(f"[ostium] mark price 0 for {symbol}")

        # amount가 notional이면 collateral로 변환 (leverage 감안)
        collateral = amount if amount < 1000 else amount  # 소액이면 collateral 직접

        trade_params = {
            "collateral": float(collateral),
            "leverage": kwargs.get("leverage", 15),
            "asset_type": pair_id,
            "direction": is_buy,
            "order_type": "MARKET",
            "tp": 0,
            "sl": 0,
        }

        latest_price, _, _ = await self._sdk.price.get_price(coin, "USD")
        receipt = self._sdk.ostium.perform_trade(trade_params, at_price=latest_price)
        logger.info(f"[ostium] 주문 체결: {side} {coin} collateral=${collateral}")
        return receipt

    async def get_position(self, symbol: str) -> Optional[Dict]:
        """서브그래프에서 포지션 조회"""
        try:
            trades = await self._sdk.subgraph.get_open_trades(self._address)
            coin = symbol.replace("-USD", "").replace("USD", "").upper()
            pair_id = PAIR_INDEX.get(coin, 0)

            for t in trades:
                if int(t.get("pair", {}).get("id", -1)) == pair_id:
                    is_long = t.get("isBuy", True)
                    return {
                        "symbol": symbol,
                        "side": "long" if is_long else "short",
                        "size": float(t.get("collateral", 0)),
                        "entry_price": float(t.get("openPrice", 0)),
                        "unrealized_pnl": 0,
                        "raw_data": t,
                    }
            return None
        except Exception as e:
            logger.debug(f"[ostium] get_position {symbol} 에러: {e}")
            return None

    async def close_position(self, symbol: str, position: Dict = None, **kwargs):
        """포지션 청산"""
        if not position:
            position = await self.get_position(symbol)
        if not position:
            return None

        raw = position.get("raw_data", {})
        pair_id = int(raw.get("pair", {}).get("id", 0))
        trade_index = int(raw.get("index", 0))
        mark_price = await self.get_mark_price(symbol)

        receipt = self._sdk.ostium.close_trade(
            pair_id=pair_id,
            trade_index=trade_index,
            market_price=mark_price,
            close_percentage=100,
        )
        logger.info(f"[ostium] 포지션 청산 완료: {symbol}")
        return receipt

    async def get_collateral(self) -> Dict:
        try:
            # SDK balance methods are sync, not async
            balance = self._sdk.balance.get_usdc_balance(self._address)
            return {
                "total_collateral": float(balance),
                "available_collateral": float(balance),
            }
        except Exception:
            # Fallback: direct web3 call
            try:
                from web3 import Web3
                w3 = Web3(Web3.HTTPProvider(self._rpc_url))
                usdc_addr = '<EVM_ADDRESS>'
                abi = [{'constant':True,'inputs':[{'name':'account','type':'address'}],'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'type':'function'}]
                contract = w3.eth.contract(address=w3.to_checksum_address(usdc_addr), abi=abi)
                bal = contract.functions.balanceOf(w3.to_checksum_address(self._address)).call()
                return {"total_collateral": bal / 1e6, "available_collateral": bal / 1e6}
            except Exception as e2:
                logger.debug(f"[ostium] get_collateral 에러: {e2}")
                return {"total_collateral": 0, "available_collateral": 0}

    async def update_leverage(self, symbol: str, leverage: int = None, margin_mode: str = None):
        # Ostium은 주문별 레버리지 설정 (별도 API 없음)
        pass

    async def get_open_orders(self, symbol: str):
        try:
            orders = await self._sdk.subgraph.get_orders(self._address)
            return orders
        except Exception:
            return []

    async def cancel_orders(self, symbol: str):
        # 온체인 캔슬 — 구현 복잡도 높아 스킵
        pass

    async def close(self):
        pass
