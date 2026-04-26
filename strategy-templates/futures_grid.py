"""
FuturesGrid Strategy — 선물 그리드 트레이딩

Minara AI FuturesGrid (pattern=3) 역엔지니어링 기반.
설정된 가격 범위 내에서 자동 그리드 매매.

핵심 특성:
- 가격 범위 (lowerPrice ~ upperPrice) 내 균일 그리드
- 각 그리드 레벨에서 지정가 주문 배치
- 횡보/레인지 시장에서 수익 극대화
- ATR 기반 동적 그리드 범위 지원
- 방향성: long-only, short-only, 또는 neutral

래퍼 인터페이스 (기존 PairTrader와 동일):
- get_mark_price(symbol) → float
- create_order(symbol, side, amount, price=None, order_type='market'/'limit')
- get_position(symbol) → dict
- close_position(symbol, position)
- update_leverage(symbol, leverage, margin_mode)
- get_open_orders(symbol) → list
- cancel_orders(symbol, orders)
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

MAKER_FEE_RATES = {
    "hyperliquid": 0.0001, "miracle": 0.0001, "nado": 0.0002,
    "hotstuff": 0.0002, "standx": 0.0002, "ethereal": 0.0002,
    "decibel": 0.0002, "treadfi.pacifica": 0.0003, "dreamcash": 0.0001,
}
TAKER_FEE_RATES = {
    "hyperliquid": 0.00035, "miracle": 0.00035, "nado": 0.0005,
    "hotstuff": 0.0005, "standx": 0.0005, "ethereal": 0.0005,
    "decibel": 0.0005, "treadfi.pacifica": 0.0006, "dreamcash": 0.00035,
}


@dataclass
class FuturesGridConfig:
    """FuturesGrid 전략 설정"""
    # 기본
    coin: str = "BTC"
    leverage: int = 15
    side: str = "long"                   # "long", "short", "neutral"
    total_margin: float = 500            # 총 투입 마진 (USD)

    # 그리드 파라미터 (Minara 구조)
    lower_price: float = 0               # 그리드 하한가 (0=자동 계산)
    upper_price: float = 0               # 그리드 상한가 (0=자동 계산)
    grid_count: int = 20                 # 그리드 수량
    auto_range: bool = True              # ATR 기반 자동 범위 설정

    # 자동 범위 설정
    auto_range_atr_mult: float = 3.0     # ATR × N으로 범위 설정

    # 스탑로스
    long_stop_loss_price: float = 0      # 롱 스탑로스 (0=비활성)
    short_stop_loss_price: float = 0     # 숏 스탑로스 (0=비활성)
    stop_loss_percent: float = 10.0      # 전체 그리드 손절 (%)

    # 주문 설정
    use_limit_order: bool = True         # 지정가 주문 사용
    post_only: bool = True               # Post-Only 강제

    # 루프 설정
    scan_interval: int = 30              # 그리드 체크 주기 (초)
    rebalance_interval: int = 300        # 리밸런싱 최소 간격 (초)
    force_rebalance_hours: float = 24.0  # 강제 리밸런싱 주기 (시간)
    range_exit_ratio: float = 0.7        # 이 비율 이상 범위 끝에 도달하면 리밸런싱

    # 리스크
    max_total_risk_ratio: float = 0.0
    trading_limit_count: int = 1         # TUI 호환 (그리드는 1로 고정)


@dataclass
class GridLevel:
    """단일 그리드 레벨"""
    price: float
    side: str        # "buy" or "sell"
    size: float
    order_id: str = ""
    filled: bool = False


class FuturesGrid:
    """선물 그리드 트레이딩 엔진"""

    def __init__(
        self,
        exchange_wrapper,
        config: FuturesGridConfig,
        exchange_name: str = "",
    ):
        self.wrapper = exchange_wrapper
        self.config = config
        self.exchange_name = exchange_name
        self.running = False
        self.observe_mode = False

        # 그리드 상태
        self.grid_levels: list[GridLevel] = []
        self.active_orders: dict = {}  # price → order_id
        self.total_pnl: float = 0.0
        self.filled_count: int = 0

        # TUI 호환 속성
        self.tag = exchange_name.upper()[:5].ljust(5)
        self.entry_count = 0
        self._fee_saved_total = 0.0
        self._maker_fills = 0
        self._taker_fills = 0
        self._maker_fee_rate = MAKER_FEE_RATES.get(exchange_name, 0.0002)
        self._taker_fee_rate = TAKER_FEE_RATES.get(exchange_name, 0.0005)

        # 로거
        self.trade_logger = None
        self._current_trade_id = None
        self._last_pnl_percent = 0.0

        # 그리드 범위
        self._actual_lower = 0.0
        self._actual_upper = 0.0
        self._grid_initialized = False
        self._last_rebalance = 0.0
        self._rebalance_count = 0

    def set_logger(self, trade_logger):
        self.trade_logger = trade_logger

    # ──────────────────────────────────────────
    # 그리드 계산
    # ──────────────────────────────────────────

    def _calculate_grid_levels(self, current_price: float, atr: float = 0) -> list[GridLevel]:
        """그리드 레벨 계산"""
        lower = self.config.lower_price
        upper = self.config.upper_price

        # 자동 범위 설정
        if self.config.auto_range or lower == 0 or upper == 0:
            if atr > 0:
                spread = atr * self.config.auto_range_atr_mult
            else:
                spread = current_price * 0.08  # 기본 ±4%
            lower = current_price - spread
            upper = current_price + spread

        if lower >= upper:
            logger.error(f"  {self.tag} │ 잘못된 그리드 범위: {lower:.2f} >= {upper:.2f}")
            return []

        self._actual_lower = lower
        self._actual_upper = upper

        count = self.config.grid_count
        step = (upper - lower) / count
        per_grid_margin = self.config.total_margin / count
        per_grid_size = per_grid_margin * self.config.leverage / current_price

        levels = []
        for i in range(count + 1):
            price = lower + step * i

            if self.config.side == "long":
                # 롱 그리드: 현재가 아래에서 매수, 위에서 매도
                if price < current_price:
                    side = "buy"
                else:
                    side = "sell"
            elif self.config.side == "short":
                # 숏 그리드: 현재가 위에서 매도, 아래에서 매수(청산)
                if price > current_price:
                    side = "sell"
                else:
                    side = "buy"
            else:
                # 뉴트럴: 아래 매수, 위 매도
                side = "buy" if price < current_price else "sell"

            levels.append(GridLevel(
                price=round(price, 2),
                side=side,
                size=round(per_grid_size, 6),
            ))

        logger.info(
            f"  {self.tag} │ 그리드 설정: {count}개 레벨 "
            f"${lower:.2f} ~ ${upper:.2f} (step=${step:.2f}) "
            f"per_grid=${per_grid_margin:.2f}"
        )

        return levels

    # ──────────────────────────────────────────
    # 주문 관리
    # ──────────────────────────────────────────

    async def _place_grid_orders(self, current_price: float):
        """그리드 주문 배치"""
        if self.observe_mode:
            return

        placed = 0
        for level in self.grid_levels:
            if level.filled or level.order_id:
                continue
            # 현재가와 너무 가까운 레벨은 스킵 (스프레드 내)
            if abs(level.price - current_price) / current_price < 0.001:
                continue

            try:
                if self.config.use_limit_order:
                    kwargs = {}
                    if self.config.post_only:
                        kwargs["tif"] = "Alo"
                    result = await self.wrapper.create_order(
                        self.config.coin, level.side, level.size,
                        price=level.price, order_type="limit", **kwargs
                    )
                    level.order_id = str(result.get("order_id", "")) if isinstance(result, dict) else str(result)
                    self._maker_fills += 1
                else:
                    # 시장가 모드 (현재가 근처의 레벨만)
                    if abs(level.price - current_price) / current_price < 0.005:
                        result = await self.wrapper.create_order(
                            self.config.coin, level.side, level.size,
                            order_type="market"
                        )
                        level.filled = True
                        level.order_id = "market"
                        self._taker_fills += 1
                        self.filled_count += 1

                placed += 1
                await asyncio.sleep(0.1)  # 레이트 리밋 방지

            except Exception as e:
                logger.debug(f"  {self.tag} │ 주문 실패 @${level.price:.2f}: {e}")

        if placed > 0:
            logger.info(f"  {self.tag} │ {placed}개 그리드 주문 배치")

    async def _check_filled_orders(self, current_price: float):
        """체결된 주문 확인 및 반대 주문 배치"""
        try:
            open_orders = await self.wrapper.get_open_orders(self.config.coin)
            open_ids = set()
            if open_orders:
                for order in open_orders:
                    oid = str(order.get("order_id", order.get("id", "")))
                    open_ids.add(oid)

            for level in self.grid_levels:
                if level.filled or not level.order_id:
                    continue
                if level.order_id == "market":
                    continue

                # 주문이 오더 리스트에 없으면 체결된 것
                if level.order_id not in open_ids:
                    level.filled = True
                    self.filled_count += 1
                    self._maker_fills += 1

                    # 체결 PnL 추적
                    grid_step = (self._actual_upper - self._actual_lower) / self.config.grid_count
                    grid_pnl = grid_step * level.size * self.config.leverage
                    self.total_pnl += grid_pnl

                    logger.info(
                        f"  {self.tag} │ 체결! @${level.price:.2f} {level.side.upper()} "
                        f"(+${grid_pnl:.2f}) 총={self.filled_count}건"
                    )

                    # 반대 방향 주문 생성 (그리드 재배치)
                    opposite_side = "sell" if level.side == "buy" else "buy"
                    if level.side == "buy":
                        opposite_price = level.price + grid_step
                    else:
                        opposite_price = level.price - grid_step

                    if self._actual_lower <= opposite_price <= self._actual_upper:
                        try:
                            kwargs = {"tif": "Alo"} if self.config.post_only else {}
                            result = await self.wrapper.create_order(
                                self.config.coin, opposite_side, level.size,
                                price=round(opposite_price, 2), order_type="limit", **kwargs
                            )
                            # 새 레벨로 추가
                            self.grid_levels.append(GridLevel(
                                price=round(opposite_price, 2),
                                side=opposite_side,
                                size=level.size,
                                order_id=str(result.get("order_id", "")) if isinstance(result, dict) else str(result),
                            ))
                        except Exception as e:
                            logger.debug(f"  {self.tag} │ 반대 주문 실패: {e}")

        except Exception as e:
            logger.error(f"  {self.tag} │ 주문 체크 실패: {e}")

    async def _check_stop_loss(self, current_price: float) -> bool:
        """스탑로스 체크"""
        # 롱 스탑로스
        if self.config.side == "long" and self.config.long_stop_loss_price > 0:
            if current_price <= self.config.long_stop_loss_price:
                logger.warning(
                    f"  {self.tag} │ 롱 스탑로스! ${current_price:.2f} <= ${self.config.long_stop_loss_price:.2f}"
                )
                return True

        # 숏 스탑로스
        if self.config.side == "short" and self.config.short_stop_loss_price > 0:
            if current_price >= self.config.short_stop_loss_price:
                logger.warning(
                    f"  {self.tag} │ 숏 스탑로스! ${current_price:.2f} >= ${self.config.short_stop_loss_price:.2f}"
                )
                return True

        # 총 PnL 기반 손절
        if self.config.stop_loss_percent > 0 and self.config.total_margin > 0:
            total_pnl_pct = (self.total_pnl / self.config.total_margin) * 100
            if total_pnl_pct <= -self.config.stop_loss_percent:
                logger.warning(
                    f"  {self.tag} │ 그리드 손절! PnL={total_pnl_pct:.2f}% <= -{self.config.stop_loss_percent}%"
                )
                return True

        return False

    async def _cancel_all_grid_orders(self):
        """모든 그리드 주문 취소"""
        try:
            orders = await self.wrapper.get_open_orders(self.config.coin)
            if orders:
                await self.wrapper.cancel_orders(self.config.coin, orders)
                logger.info(f"  {self.tag} │ {len(orders)}개 그리드 주문 취소")
        except Exception as e:
            logger.error(f"  {self.tag} │ 주문 취소 실패: {e}")

    async def _close_all_positions(self):
        """모든 포지션 청산"""
        try:
            pos = await self.wrapper.get_position(self.config.coin)
            if pos and float(pos.get("size", 0)) > 0:
                await self.wrapper.close_position(self.config.coin, pos)
                self._taker_fills += 1
                logger.info(f"  {self.tag} │ 포지션 청산 완료")
        except Exception as e:
            logger.error(f"  {self.tag} │ 포지션 청산 실패: {e}")

    async def _should_rebalance(self, current_price: float) -> bool:
        """
        그리드 리밸런싱 필요 여부 판단.

        트리거 조건 (OR):
        1. 강제 리밸런싱: force_rebalance_hours 경과
        2. 범위 이탈: 가격이 그리드 범위 밖으로 나감
        3. 범위 경계 접근: range_exit_ratio(70%) 이상 한쪽 끝에 치우침

        최소 간격: rebalance_interval초 (디폴트 5분)
        """
        now = time.time()
        elapsed = now - self._last_rebalance

        # 최소 간격 미충족
        if elapsed < self.config.rebalance_interval:
            return False

        grid_range = self._actual_upper - self._actual_lower
        if grid_range <= 0:
            return False

        # 1. 강제 리밸런싱 (24시간 디폴트)
        force_seconds = self.config.force_rebalance_hours * 3600
        if force_seconds > 0 and elapsed >= force_seconds:
            logger.info(
                f"  {self.tag} │ 강제 리밸런싱 ({self.config.force_rebalance_hours}h 경과)"
            )
            return True

        # 2. 가격이 그리드 범위 밖으로 벗어남
        if current_price < self._actual_lower or current_price > self._actual_upper:
            logger.info(
                f"  {self.tag} │ 범위 이탈! ${current_price:.2f} "
                f"(범위: ${self._actual_lower:.2f}~${self._actual_upper:.2f})"
            )
            return True

        # 3. 범위 경계에 너무 가까움 (70% 지점 이상)
        mid = (self._actual_lower + self._actual_upper) / 2
        distance_from_mid = abs(current_price - mid)
        half_range = grid_range / 2
        position_ratio = distance_from_mid / half_range if half_range > 0 else 0

        if position_ratio >= self.config.range_exit_ratio:
            direction = "상단" if current_price > mid else "하단"
            logger.info(
                f"  {self.tag} │ 범위 {direction} {position_ratio:.0%} 접근 "
                f"(임계={self.config.range_exit_ratio:.0%}) → 리밸런싱"
            )
            return True

        return False

    # ──────────────────────────────────────────
    # 메인 루프
    # ──────────────────────────────────────────

    async def run(self, saved_state: dict = None):
        """메인 그리드 트레이딩 루프"""
        self.running = True

        if saved_state:
            self.restore_state(saved_state)

        logger.info(
            f"  {self.tag} │ FuturesGrid 시작 | {self.config.coin} {self.config.side.upper()} | "
            f"Lev={self.config.leverage}x Grid={self.config.grid_count} "
            f"Margin=${self.config.total_margin}"
        )

        try:
            # 레버리지 설정
            await self.wrapper.update_leverage(self.config.coin, self.config.leverage, "cross")
        except Exception:
            pass

        while self.running:
            try:
                # 1. 현재가 조회
                current_price = await self.wrapper.get_mark_price(self.config.coin)

                # 2. 그리드 초기화 (최초 실행 또는 리밸런싱)
                needs_rebalance = not self._grid_initialized or await self._should_rebalance(current_price)
                if needs_rebalance:
                    if self._grid_initialized:
                        # 리밸런싱: 기존 주문 취소 후 재배치
                        self._rebalance_count += 1
                        logger.info(
                            f"  {self.tag} │ 리밸런싱 #{self._rebalance_count} "
                            f"(기존 체결={self.filled_count}건, PnL=${self.total_pnl:.2f})"
                        )
                        await self._cancel_all_grid_orders()
                        # 포지션은 유지 — 그리드만 현재가 기준으로 재설정

                    self.grid_levels = self._calculate_grid_levels(current_price)
                    if not self.grid_levels:
                        logger.error(f"  {self.tag} │ 그리드 계산 실패")
                        await asyncio.sleep(self.config.scan_interval)
                        continue

                    await self._place_grid_orders(current_price)
                    self._grid_initialized = True
                    self._last_rebalance = time.time()

                    if self.trade_logger:
                        self._current_trade_id = self.trade_logger.log_entry(
                            exchange=self.exchange_name,
                            strategy="futures_grid",
                            direction=self.config.side,
                            coin=self.config.coin,
                            price=current_price,
                            margin=self.config.total_margin,
                        )

                # 3. 체결 확인 + 반대 주문 배치
                await self._check_filled_orders(current_price)

                # 4. 스탑로스 체크
                if await self._check_stop_loss(current_price):
                    await self._cancel_all_grid_orders()
                    await self._close_all_positions()

                    if self.trade_logger and self._current_trade_id:
                        pnl_pct = (self.total_pnl / self.config.total_margin * 100) if self.config.total_margin else 0
                        self.trade_logger.log_exit(
                            trade_id=self._current_trade_id,
                            price=current_price,
                            pnl_percent=pnl_pct,
                            reason="스탑로스",
                        )
                    self.running = False
                    break

                # 5. PnL 로그
                pnl_pct = (self.total_pnl / self.config.total_margin * 100) if self.config.total_margin else 0
                self._last_pnl_percent = pnl_pct
                active = sum(1 for l in self.grid_levels if l.order_id and not l.filled)
                logger.debug(
                    f"  {self.tag} │ Grid: ${self._actual_lower:.0f}~${self._actual_upper:.0f} "
                    f"활성={active} 체결={self.filled_count} PnL=${self.total_pnl:.2f} ({pnl_pct:.2f}%)"
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"  {self.tag} │ 사이클 에러: {e}", exc_info=True)

            await asyncio.sleep(self.config.scan_interval)

    async def shutdown(self, close_positions: bool = True):
        """종료"""
        self.running = False
        if close_positions:
            await self._cancel_all_grid_orders()
            await self._close_all_positions()

    def get_state(self) -> dict:
        """상태 저장"""
        return {
            "strategy": "futures_grid",
            "exchange_name": self.exchange_name,
            "total_pnl": self.total_pnl,
            "filled_count": self.filled_count,
            "lower": self._actual_lower,
            "upper": self._actual_upper,
            "grid_initialized": self._grid_initialized,
            "rebalance_count": self._rebalance_count,
            "last_rebalance": self._last_rebalance,
        }

    def restore_state(self, state: dict):
        """상태 복원"""
        if not state or state.get("strategy") != "futures_grid":
            return
        self.total_pnl = state.get("total_pnl", 0)
        self.filled_count = state.get("filled_count", 0)
        self._rebalance_count = state.get("rebalance_count", 0)
        self._last_rebalance = state.get("last_rebalance", 0)
        if state.get("lower") and state.get("upper"):
            self._actual_lower = state["lower"]
            self._actual_upper = state["upper"]
        logger.info(
            f"  {self.tag} │ 상태 복원: PnL=${self.total_pnl:.2f} 체결={self.filled_count} "
            f"리밸런싱={self._rebalance_count}회"
        )
