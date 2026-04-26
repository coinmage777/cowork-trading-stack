"""
Donchian Breakout ATR Strategy — 추세추종 방향성 트레이딩

Minara 백테스트 기반: BTC 1h +51.15%, Sharpe 3.39, MDD 11.67%, PF 3.31

핵심 로직:
- Donchian Channel (15봉) 돌파 진입 (1봉 오프셋으로 선행편향 방지)
- 횡보 필터: 최근 5봉 변동폭 < 2x ATR이면 진입 차단
- ATR 기반 손절(1.5x) / 익절(3x)
- 역방향 돌파 시 포지션 반전

래퍼 인터페이스 (SharpeGuardV2와 동일):
- get_mark_price(symbol) -> float
- create_order(symbol, side, amount, price=None, order_type='market')
- get_position(symbol) -> dict
- close_position(symbol, position)
- update_leverage(symbol, leverage, margin_mode)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .candle_fetcher import CandleFetcher
from .momentum import calculate_ma, calculate_rsi

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
class DonchianConfig:
    coins: list = field(default_factory=lambda: ["BTC"])
    leverage: int = 10
    trading_margin: float = 50

    # Donchian Channel
    donchian_length: int = 15
    donchian_offset: int = 1  # 1봉 오프셋 (선행편향 방지)

    # ATR
    atr_period: int = 14
    atr_sl_multiplier: float = 1.5
    atr_tp_multiplier: float = 3.0

    # 횡보 필터
    squeeze_bars: int = 5
    squeeze_atr_mult: float = 2.0

    # 고정 백업 SL/TP
    max_stop_loss: float = 5.0
    max_take_profit: float = 15.0

    # 트레일링
    trailing_enabled: bool = True
    trailing_activation: float = 2.0
    trailing_callback: float = 1.5

    # 스캔
    chart_time: int = 60  # 분 (1h)
    candle_limit: int = 200
    min_candles: int = 30
    scan_interval: int = 60  # 초
    no_entry_hours: list = field(default_factory=list)


@dataclass
class DonchianPosition:
    coin: str = ""
    side: str = ""  # "long" or "short"
    entry_price: float = 0.0
    size: float = 0.0
    margin: float = 0.0
    atr_at_entry: float = 0.0
    peak_pnl: float = 0.0
    trailing_active: bool = False
    entry_time: float = 0.0


def _get_float(candle: dict, key: str) -> float:
    return float(candle.get(key, candle.get(key[0], 0)))


def _calculate_atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    true_ranges = []
    for i in range(-period, 0):
        high = _get_float(candles[i], "high")
        low = _get_float(candles[i], "low")
        prev_close = _get_float(candles[i - 1], "close")
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    return sum(true_ranges) / len(true_ranges)


def _calculate_donchian(candles: list[dict], length: int, offset: int = 1):
    """Donchian Channel (upper, lower, mid) 계산. offset=1이면 현재봉 제외."""
    n = len(candles)
    end_idx = n - offset
    start_idx = end_idx - length
    if start_idx < 0 or end_idx <= 0:
        return None, None, None
    subset = candles[start_idx:end_idx]
    if len(subset) < length:
        return None, None, None
    highs = [_get_float(c, "high") for c in subset]
    lows = [_get_float(c, "low") for c in subset]
    upper = max(highs)
    lower = min(lows)
    mid = (upper + lower) / 2
    return upper, lower, mid


class DonchianBreakout:
    """Donchian Channel Breakout + ATR 전략 엔진"""

    def __init__(
        self,
        exchange_wrapper,
        candle_fetcher: CandleFetcher,
        config: DonchianConfig,
        exchange_name: str = "",
    ):
        self.wrapper = exchange_wrapper
        self.candle_fetcher = candle_fetcher
        self.config = config
        self.exchange_name = exchange_name
        self.running = False
        self.observe_mode = False

        # 코인별 포지션
        self.positions: dict[str, DonchianPosition] = {}
        # 이전 봉 Donchian 값 (크로스오버 감지)
        self._prev_upper: dict[str, float] = {}
        self._prev_lower: dict[str, float] = {}

        self.tag = exchange_name.upper()[:5].ljust(5)
        self._taker_fills = 0
        self._maker_fills = 0

        self.trade_logger = None

    def set_logger(self, trade_logger):
        self.trade_logger = trade_logger

    # ──────────────────────────────────────────
    # 시그널
    # ──────────────────────────────────────────

    def _evaluate(self, candles: list[dict], coin: str) -> dict:
        """
        Donchian 돌파 시그널 평가.
        Returns: {"direction": "long"|"short"|None, "upper", "lower", "mid", "atr", "squeeze"}
        """
        cfg = self.config
        upper, lower, mid = _calculate_donchian(candles, cfg.donchian_length, cfg.donchian_offset)
        if upper is None:
            return {"direction": None}

        atr = _calculate_atr(candles, cfg.atr_period)
        close = _get_float(candles[-1], "close")
        prev_close = _get_float(candles[-2], "close")

        # 횡보 필터: 최근 N봉의 high-low range < squeeze_atr_mult * ATR
        recent = candles[-cfg.squeeze_bars:]
        recent_range = max(_get_float(c, "high") for c in recent) - min(_get_float(c, "low") for c in recent)
        is_squeeze = recent_range < cfg.squeeze_atr_mult * atr if atr > 0 else True

        # 크로스오버 감지: 이전 close가 채널 안에 있다가 현재 close가 돌파
        prev_upper = self._prev_upper.get(coin, upper)
        prev_lower = self._prev_lower.get(coin, lower)

        direction = None
        if not is_squeeze:
            # 상단 돌파: close > upper AND prev_close <= prev_upper
            if close > upper and prev_close <= prev_upper:
                direction = "long"
            # 하단 돌파: close < lower AND prev_close >= prev_lower
            elif close < lower and prev_close >= prev_lower:
                direction = "short"

        # 캐시 업데이트
        self._prev_upper[coin] = upper
        self._prev_lower[coin] = lower

        return {
            "direction": direction,
            "upper": upper,
            "lower": lower,
            "mid": mid,
            "atr": atr,
            "close": close,
            "squeeze": is_squeeze,
        }

    # ──────────────────────────────────────────
    # 진입/청산
    # ──────────────────────────────────────────

    def _calc_pnl(self, pos: DonchianPosition, price: float) -> float:
        if not pos.side or pos.entry_price == 0:
            return 0.0
        if pos.side == "long":
            return (price / pos.entry_price - 1) * 100
        return (1 - price / pos.entry_price) * 100

    async def _enter(self, coin: str, direction: str, price: float, atr: float):
        if self.observe_mode:
            return
        current_hour = datetime.utcnow().hour
        if current_hour in self.config.no_entry_hours:
            return

        side = "buy" if direction == "long" else "sell"
        margin = self.config.trading_margin
        amount = margin * self.config.leverage / price

        try:
            try:
                await self.wrapper.update_leverage(coin, self.config.leverage, "cross")
            except Exception:
                pass

            atr_pct = (atr / price * 100) if price > 0 else 0
            sl = min(self.config.max_stop_loss, atr_pct * self.config.atr_sl_multiplier)
            tp = min(self.config.max_take_profit, atr_pct * self.config.atr_tp_multiplier)

            logger.info(
                f"  {self.tag} │ DCH {direction.upper()} {coin} "
                f"@${price:,.1f} | ATR={atr:.1f} SL={sl:.1f}% TP={tp:.1f}%"
            )

            await self.wrapper.create_order(coin, side, amount, order_type="market")
            self._taker_fills += 1

            self.positions[coin] = DonchianPosition(
                coin=coin,
                side=direction,
                entry_price=price,
                size=amount,
                margin=margin,
                atr_at_entry=atr,
                entry_time=time.time(),
            )

            if self.trade_logger:
                self.trade_logger.log_entry(
                    exchange=self.exchange_name,
                    strategy="donchian_breakout",
                    direction=direction,
                    coin=coin,
                    price=price,
                    margin=margin,
                )

        except Exception as e:
            logger.error(f"  {self.tag} │ DCH 진입 실패 {coin}: {e}")

    async def _close(self, coin: str, price: float, reason: str):
        pos = self.positions.get(coin)
        if not pos or not pos.side:
            return
        pnl = self._calc_pnl(pos, price)
        try:
            result_icon = "+" if pnl >= 0 else "-"
            logger.info(
                f"  {self.tag} │ DCH 청산 {coin} {pos.side.upper()} "
                f"PnL={pnl:+.2f}% | {reason}"
            )
            real_pos = await self.wrapper.get_position(coin)
            if real_pos and float(real_pos.get("size", 0)) > 0:
                await self.wrapper.close_position(coin, real_pos)
                self._taker_fills += 1

            if self.trade_logger:
                self.trade_logger.log_exit(
                    trade_id=None,
                    price=price,
                    pnl_percent=pnl,
                    reason=reason,
                )

        except Exception as e:
            logger.error(f"  {self.tag} │ DCH 청산 실패 {coin}: {e}")
        finally:
            self.positions[coin] = DonchianPosition()

    async def _check_exit(self, coin: str, signal: dict):
        pos = self.positions.get(coin)
        if not pos or not pos.side:
            return

        price = signal["close"]
        pnl = self._calc_pnl(pos, price)
        atr = signal.get("atr", pos.atr_at_entry)
        atr_pct = (atr / price * 100) if price > 0 else 0

        dynamic_sl = min(self.config.max_stop_loss, atr_pct * self.config.atr_sl_multiplier)
        dynamic_tp = min(self.config.max_take_profit, atr_pct * self.config.atr_tp_multiplier)

        reason = None

        # 1. 손절
        if pnl <= -dynamic_sl:
            reason = f"SL {pnl:.2f}%"

        # 2. 트레일링
        elif self.config.trailing_enabled:
            if pnl > pos.peak_pnl:
                pos.peak_pnl = pnl
            if pnl >= self.config.trailing_activation:
                pos.trailing_active = True
            if pos.trailing_active:
                dd = pos.peak_pnl - pnl
                if dd >= self.config.trailing_callback:
                    reason = f"Trail {pnl:.2f}% (peak={pos.peak_pnl:.2f}%)"

        # 3. 고정 익절 (트레일링 미활성 시)
        elif pnl >= dynamic_tp:
            reason = f"TP {pnl:.2f}%"

        # 4. 역방향 돌파 → 반전
        if not reason and signal.get("direction") and signal["direction"] != pos.side:
            reason = f"반전 → {signal['direction'].upper()}"

        if reason:
            reverse_dir = signal.get("direction") if "반전" in (reason or "") else None
            await self._close(coin, price, reason)
            # 반전 진입
            if reverse_dir:
                await self._enter(coin, reverse_dir, price, signal.get("atr", 0))

    # ──────────────────────────────────────────
    # 메인 루프
    # ──────────────────────────────────────────

    async def run(self, saved_state: dict = None):
        self.running = True
        if saved_state:
            self.restore_state(saved_state)

        coins = self.config.coins
        logger.info(
            f"  {self.tag} │ Donchian Breakout 시작 | "
            f"coins={','.join(coins)} Lev={self.config.leverage}x "
            f"DC={self.config.donchian_length} ATR_SL={self.config.atr_sl_multiplier}x "
            f"ATR_TP={self.config.atr_tp_multiplier}x"
        )

        while self.running:
            for coin in coins:
                try:
                    candles = await self.candle_fetcher.get_candles(
                        coin,
                        interval=self.config.chart_time,
                        limit=self.config.candle_limit,
                    )
                    if not candles or len(candles) < self.config.min_candles:
                        continue

                    signal = self._evaluate(candles, coin)
                    await self._sync_position(coin)

                    pos = self.positions.get(coin)
                    if pos and pos.side:
                        await self._check_exit(coin, signal)
                    elif signal.get("direction") and not signal.get("squeeze"):
                        await self._enter(coin, signal["direction"], signal["close"], signal.get("atr", 0))
                    else:
                        sq = "SQ" if signal.get("squeeze") else ""
                        upper = signal.get("upper", 0)
                        lower = signal.get("lower", 0)
                        close = signal.get("close", 0)
                        logger.info(
                            f"  {self.tag} │ DCH {coin} 대기 | "
                            f"C={close:,.1f} U={upper:,.1f} L={lower:,.1f} {sq}"
                        )

                except asyncio.CancelledError:
                    self.running = False
                    return
                except Exception as e:
                    logger.error(f"  {self.tag} │ DCH {coin} 에러: {e}")

            await asyncio.sleep(self.config.scan_interval)

    async def _sync_position(self, coin: str):
        try:
            real_pos = await self.wrapper.get_position(coin)
            pos = self.positions.get(coin, DonchianPosition())
            if real_pos and float(real_pos.get("size", 0)) > 0:
                if not pos.side:
                    pos.coin = coin
                    pos.side = real_pos.get("side", "long")
                    pos.entry_price = float(real_pos.get("entry_price", 0))
                    pos.size = float(real_pos.get("size", 0))
                    pos.entry_time = time.time()
                    self.positions[coin] = pos
                    logger.info(f"  {self.tag} │ DCH {coin} 기존 포지션 감지: {pos.side}")
            elif pos.side:
                logger.info(f"  {self.tag} │ DCH {coin} 외부 청산 감지")
                self.positions[coin] = DonchianPosition()
        except Exception:
            pass

    async def shutdown(self, close_positions: bool = True):
        self.running = False
        if close_positions:
            for coin, pos in list(self.positions.items()):
                if pos.side:
                    try:
                        price = await self.wrapper.get_mark_price(coin)
                        await self._close(coin, price, "shutdown")
                    except Exception as e:
                        logger.error(f"  {self.tag} │ DCH {coin} 종료 청산 실패: {e}")

    def get_state(self) -> dict:
        states = {}
        for coin, pos in self.positions.items():
            if pos.side:
                states[coin] = {
                    "side": pos.side,
                    "entry_price": pos.entry_price,
                    "size": pos.size,
                    "margin": pos.margin,
                    "atr_at_entry": pos.atr_at_entry,
                    "peak_pnl": pos.peak_pnl,
                    "trailing_active": pos.trailing_active,
                }
        if not states:
            return {}
        return {"strategy": "donchian_breakout", "positions": states}

    def restore_state(self, state: dict):
        if not state or state.get("strategy") != "donchian_breakout":
            return
        for coin, s in state.get("positions", {}).items():
            self.positions[coin] = DonchianPosition(
                coin=coin,
                side=s.get("side", ""),
                entry_price=s.get("entry_price", 0),
                size=s.get("size", 0),
                margin=s.get("margin", 0),
                atr_at_entry=s.get("atr_at_entry", 0),
                peak_pnl=s.get("peak_pnl", 0),
                trailing_active=s.get("trailing_active", False),
                entry_time=time.time(),
            )
            logger.info(
                f"  {self.tag} │ DCH {coin} 상태 복원: "
                f"{s['side'].upper()} @${s['entry_price']:,.1f}"
            )
