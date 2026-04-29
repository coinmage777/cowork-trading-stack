"""
Hotstuff Exchange Wrapper
=========================
hotstuff-python-sdk 기반 래퍼.
Symbol format: BTC-PERP, ETH-PERP
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor

from eth_account import Account

logger = logging.getLogger(__name__)

# SDK는 동기 호출 → ThreadPoolExecutor로 async 래핑
_executor = ThreadPoolExecutor(max_workers=4)


class HotstuffExchange:
    """Hotstuff Perps DEX wrapper"""

    def __init__(
        self,
        private_key: str,
        is_testnet: bool = False,
    ):
        self.private_key = private_key
        self.is_testnet = is_testnet
        self._info = None
        self._exchange = None
        self._wallet = None
        self._wallet_address = None
        self._instruments: Dict[str, int] = {}  # symbol → instrumentId
        self._instrument_details: Dict[int, dict] = {}  # instrumentId → details

    async def init(self) -> "HotstuffExchange":
        from hotstuff import InfoClient, ExchangeClient

        pk = self.private_key
        if pk.startswith("0x"):
            pk = pk[2:]
        self._wallet = Account.from_key(bytes.fromhex(pk))
        self._wallet_address = self._wallet.address

        self._info = InfoClient(websocket=False, is_testnet=self.is_testnet)

        # agent 키가 있으면 agent로 거래, 없으면 자동 등록
        agent_wallet = await self._ensure_agent()

        self._exchange = ExchangeClient(
            websocket=False, is_testnet=self.is_testnet, wallet=agent_wallet
        )

        # instrument 매핑 로드
        await self._load_instruments()

        logger.debug(
            f"[hotstuff] 초기화 완료: {self._wallet_address[:10]}... "
            f"instruments={list(self._instruments.keys())}"
        )
        return self

    async def _ensure_agent(self) -> Account:
        """agent 키 확인/등록 — ExchangeClient는 agent wallet로 서명해야 함"""
        from hotstuff import ExchangeClient, AddAgentParams, AgentsParams
        import json, os

        # agent 키 파일 경로 (지갑별로 분리하여 다중 인스턴스 지원)
        agent_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", f".hotstuff_agent_{self._wallet_address.lower()}.json"
        )
        agent_file = os.path.normpath(agent_file)
        # legacy 단일 파일 fallback (이전 버전 호환)
        legacy_file = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", ".hotstuff_agent.json"
        ))
        if not os.path.exists(agent_file) and os.path.exists(legacy_file):
            try:
                with open(legacy_file, "r") as lf:
                    legacy_data = json.load(lf)
                if str(legacy_data.get("wallet_address", "")).lower() == self._wallet_address.lower():
                    agent_file = legacy_file
            except Exception:
                pass

        # 저장된 agent 키가 있으면 로드
        if os.path.exists(agent_file):
            try:
                with open(agent_file, "r") as f:
                    data = json.load(f)
                agent_pk = data["agent_private_key"]
                if agent_pk.startswith("0x"):
                    agent_pk = agent_pk[2:]
                agent_acct = Account.from_key(bytes.fromhex(agent_pk))

                # 등록 확인
                loop = asyncio.get_event_loop()
                agents = await loop.run_in_executor(
                    _executor,
                    lambda: self._info.agents(AgentsParams(user=self._wallet_address))
                )
                agent_addrs = [
                    str(self._extract(a, "agent_address", "")).lower()
                    for a in (agents if isinstance(agents, list) else [])
                ]
                if agent_acct.address.lower() in agent_addrs:
                    logger.debug(f"[hotstuff] agent 로드: {agent_acct.address[:10]}...")
                    return agent_acct
            except Exception as e:
                logger.debug(f"[hotstuff] agent 파일 로드 실패: {e}")

        # 새 agent 등록
        logger.info("[hotstuff] 새 agent 등록 중...")
        new_agent = Account.create()
        agent_pk_hex = new_agent.key.hex()
        if agent_pk_hex.startswith("0x"):
            agent_pk_hex = agent_pk_hex[2:]

        tmp_exchange = ExchangeClient(
            wallet=self._wallet, websocket=False, is_testnet=self.is_testnet
        )

        valid_until = int(time.time() * 1000) + (365 * 24 * 3600 * 1000)
        params = AddAgentParams(
            agentName="mpdex_bot",
            agent=new_agent.address,
            forAccount=self._wallet_address,
            validUntil=valid_until,
            agentPrivateKey=agent_pk_hex,
            signer=self._wallet_address,
        )

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _executor, lambda: tmp_exchange.add_agent(params)
        )
        logger.info(f"[hotstuff] agent 등록 완료: {new_agent.address[:10]}... result={result}")

        # 저장
        with open(agent_file, "w") as f:
            json.dump({
                "wallet_address": self._wallet_address,
                "agent_address": new_agent.address,
                "agent_private_key": f"0x{agent_pk_hex}",
            }, f, indent=2)

        return new_agent

    def _extract(self, obj, key, default=None):
        """typed object 또는 dict에서 값 추출"""
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    async def _load_instruments(self):
        """instruments API로 심볼 → instrumentId 매핑 구축"""
        from hotstuff import InstrumentsParams

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _executor, lambda: self._info.instruments(InstrumentsParams(type="perps"))
        )

        # SDK returns dict {'perps': [...]} or InstrumentsResponse with .perps attr
        if isinstance(result, dict):
            perps = result.get("perps", [])
        else:
            perps = getattr(result, "perps", None)
            if perps is None:
                perps = result if isinstance(result, list) else []

        for inst in perps:
            if isinstance(inst, dict):
                name = inst.get("name", "")
                iid = inst.get("id")
            else:
                name = getattr(inst, "name", "")
                iid = getattr(inst, "id", None)
            if name and iid is not None:
                self._instruments[name] = int(iid)
                self._instrument_details[int(iid)] = inst

        logger.debug(f"[hotstuff] instruments loaded: {self._instruments}")

    def _resolve_instrument_id(self, symbol: str) -> int:
        """symbol(BTC-PERP 또는 BTC) → instrumentId"""
        # 직접 매칭
        if symbol in self._instruments:
            return self._instruments[symbol]
        # BTC → BTC-PERP
        perp_symbol = f"{symbol}-PERP"
        if perp_symbol in self._instruments:
            return self._instruments[perp_symbol]
        # 대소문자 무시
        for name, iid in self._instruments.items():
            if name.upper() == symbol.upper() or name.upper() == perp_symbol.upper():
                return iid
        raise ValueError(f"[hotstuff] Unknown symbol: {symbol}, available: {list(self._instruments.keys())}")

    def _to_perp_symbol(self, symbol: str) -> str:
        """BTC → BTC-PERP"""
        if "-PERP" in symbol.upper():
            return symbol.upper()
        return f"{symbol.upper()}-PERP"

    def _round_to_tick(self, value: float, tick: float) -> float:
        """tick size에 맞게 반올림"""
        import math
        if tick <= 0:
            return value
        # tick의 소수점 자릿수 계산
        decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
        return round(round(value / tick) * tick, decimals)

    def _get_tick_lot(self, symbol: str):
        """심볼의 tick_size, lot_size 반환"""
        instrument_id = self._resolve_instrument_id(symbol)
        details = self._instrument_details.get(instrument_id, {})
        tick = float(details.get("tick_size", 1) if isinstance(details, dict) else getattr(details, "tick_size", 1))
        lot = float(details.get("lot_size", 0.001) if isinstance(details, dict) else getattr(details, "lot_size", 0.001))
        return tick, lot

    # ==============================================================
    # MultiPerpDex Interface
    # ==============================================================

    async def get_mark_price(self, symbol: str, **kwargs) -> Optional[float]:
        """마크 가격 조회 (bbo API 사용 — mids는 SDK MidsParams에 symbol 필드 없어서 400 에러)"""
        from hotstuff.methods.info.market import BBOParams

        loop = asyncio.get_event_loop()
        perp_sym = self._to_perp_symbol(symbol)
        try:
            result = await loop.run_in_executor(
                _executor, lambda: self._info.bbo(BBOParams(symbol=perp_sym))
            )
            if isinstance(result, list) and result:
                item = result[0]
                bid = float(self._extract(item, "best_bid_price", 0))
                ask = float(self._extract(item, "best_ask_price", 0))
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2
                return bid or ask or None
        except Exception as e:
            logger.debug(f"[hotstuff] get_mark_price error: {e}")
        return None

    async def create_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float = None,
        order_type: str = "market",
        **kwargs,
    ) -> dict:
        """주문 생성"""
        from hotstuff import PlaceOrderParams, UnitOrder

        instrument_id = self._resolve_instrument_id(symbol)
        is_market = order_type.lower() == "market"
        hs_side = "b" if side.lower() in ("buy", "long") else "s"
        reduce_only = kwargs.get("reduce_only", False)
        tick_size, lot_size = self._get_tick_lot(symbol)

        # 마켓 주문: 가격을 시장가 기준으로 설정 (슬리피지 ±2%)
        if is_market and price is None:
            mark = await self.get_mark_price(symbol)
            if mark:
                if hs_side == "b":
                    price = mark * 1.02
                else:
                    price = mark * 0.98

        if price is None:
            raise ValueError(f"[hotstuff] price required for order on {symbol}")

        # tick_size / lot_size에 맞춤
        price = self._round_to_tick(price, tick_size)
        amount = self._round_to_tick(amount, lot_size)

        unit_order = UnitOrder(
            instrumentId=instrument_id,
            side=hs_side,
            positionSide="BOTH",
            price=str(price),
            size=str(amount),
            tif="IOC" if is_market else "GTC",
            ro=reduce_only,
            po=False,
            isMarket=is_market,
        )

        expires_after = int(time.time() * 1000) + 60000  # 1분
        params = PlaceOrderParams(orders=[unit_order], expiresAfter=expires_after)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _executor, lambda: self._exchange.place_order(params)
        )

        logger.debug(f"[hotstuff] order result: {result}")
        return result if isinstance(result, dict) else {"result": result}

    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """포지션 조회"""
        from hotstuff import PositionsParams

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                _executor,
                lambda: self._info.positions(PositionsParams(user=self._wallet_address)),
            )

            perp_symbol = self._to_perp_symbol(symbol)
            instrument_id = self._resolve_instrument_id(symbol)

            # SDK may return typed list or plain list
            positions = result if isinstance(result, list) else getattr(result, "positions", result) if result else []
            if not isinstance(positions, list):
                positions = [positions] if positions else []

            for pos in positions:
                # match by instrumentId or symbol name (typed object or dict)
                pos_iid = self._extract(pos, "instrumentId") or self._extract(pos, "instrument_id")
                pos_sym = self._extract(pos, "symbol") or ""
                if (pos_iid is not None and int(pos_iid) == instrument_id) or \
                   str(pos_sym).upper() == perp_symbol:
                    size = float(self._extract(pos, "size", 0))
                    if size == 0:
                        continue
                    side = "long" if size > 0 else "short"
                    return {
                        "symbol": symbol.upper(),
                        "side": side,
                        "size": abs(size),
                        "entry_price": float(self._extract(pos, "entryPrice") or self._extract(pos, "entry_price") or 0),
                        "unrealized_pnl": float(self._extract(pos, "unrealizedPnl") or self._extract(pos, "upnl") or 0),
                        "liquidation_price": self._extract(pos, "liquidationPrice") or self._extract(pos, "liq_price"),
                        "raw_data": pos,
                    }
        except Exception as e:
            logger.debug(f"[hotstuff] get_position error: {e}")

        return None

    async def close_position(self, symbol: str, position=None, **kwargs):
        """포지션 청산 (반대 방향 마켓 주문 + reduce_only)"""
        if position is None:
            position = await self.get_position(symbol)
        if not position or float(position.get("size", 0)) == 0:
            return None

        side = "sell" if position["side"] == "long" else "buy"
        size = float(position["size"])

        return await self.create_order(
            symbol=symbol,
            side=side,
            amount=size,
            order_type="market",
            reduce_only=True,
        )

    async def get_collateral(self) -> Dict[str, Any]:
        """잔고/담보 조회"""
        from hotstuff import AccountSummaryParams

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                _executor,
                lambda: self._info.account_summary(
                    AccountSummaryParams(user=self._wallet_address)
                ),
            )

            # SDK returns AccountSummaryResponse with .total_account_equity
            equity = (
                self._extract(result, "total_account_equity")
                or self._extract(result, "equity")
                or self._extract(result, "totalEquity")
                or self._extract(result, "accountEquity")
                or self._extract(result, "total_collateral")
                or 0
            )
            return {
                "total_collateral": float(equity) if equity else 0,
                "raw_data": result,
            }
        except Exception as e:
            logger.debug(f"[hotstuff] get_collateral error: {e}")
            return {"total_collateral": 0}

    async def get_balance(self) -> float:
        """잔고 조회 (오토스케일러용)"""
        coll = await self.get_collateral()
        return coll.get("total_collateral", 0)

    async def update_leverage(self, symbol: str, leverage=None, margin_mode=None):
        """레버리지 설정"""
        from hotstuff import UpdatePerpInstrumentLeverageParams

        if leverage is None:
            return

        instrument_id = self._resolve_instrument_id(symbol)
        params = UpdatePerpInstrumentLeverageParams(
            instrumentId=instrument_id, leverage=str(int(leverage))
        )

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                _executor, lambda: self._exchange.update_perp_instrument_leverage(params)
            )
            logger.debug(f"[hotstuff] leverage updated: {symbol} → {leverage}x")
            return result
        except Exception as e:
            logger.debug(f"[hotstuff] update_leverage error: {e}")

    async def get_open_orders(self, symbol: str):
        """미체결 주문 조회"""
        from hotstuff import OpenOrdersParams

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                _executor,
                lambda: self._info.open_orders(
                    OpenOrdersParams(user=self._wallet_address)
                ),
            )
            instrument_id = self._resolve_instrument_id(symbol)
            orders = result if isinstance(result, list) else getattr(result, "orders", []) or []
            return [
                o for o in orders
                if int(self._extract(o, "instrumentId") or self._extract(o, "instrument_id") or -1) == instrument_id
            ]
        except Exception as e:
            logger.debug(f"[hotstuff] get_open_orders error: {e}")
            return []

    async def cancel_orders(self, symbol: str):
        """전체 주문 취소"""
        from hotstuff import CancelAllParams

        loop = asyncio.get_event_loop()
        try:
            params = CancelAllParams()
            result = await loop.run_in_executor(
                _executor, lambda: self._exchange.cancel_all(params)
            )
            logger.debug(f"[hotstuff] cancel_all result: {result}")
            return result
        except Exception as e:
            logger.debug(f"[hotstuff] cancel_orders error: {e}")
            return None

    async def close(self):
        """세션 정리"""
        logger.debug("[hotstuff] closed")
