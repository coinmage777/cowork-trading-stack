"""
Pair Trader V3 - VARIATIONAL_PAIR_V3 Strategy
BTC-ETH 쌍 트레이딩 (ETHBTC 스프레드 기반)

전략 로직:
1. 모멘텀 스코어 계산 (ROC + MA 기반, 5-factor)
2. 방향 결정: 높은 모멘텀 LONG, 낮은 모멘텀 SHORT
3. 연속 캔들 분석: 5개 연속 베어/불 캔들 → 진입 신호
4. ATR 필터: 변동성 과도 시 필터링
5. DCA: 이중 조건 만족 시 추가 진입
6. 청산: Take Profit / Stop Loss / MA Crossover

래퍼 인터페이스 (MultiPerpDex base.py 기준):
- get_mark_price(symbol) → float
- create_order(symbol, side, amount, price=None, order_type='market')
- get_position(symbol) → dict {side, size, entry_price, ...}
- close_position(symbol, position)
- update_leverage(symbol, leverage, margin_mode)
"""

import asyncio
import aiohttp
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────
# 수수료율 상수 (거래소별)
# ──────────────────────────────────────────
MAKER_FEE_RATES = {
    "hyperliquid": 0.0001,
    "miracle": 0.0001,
    "nado": 0.0002,
    "hotstuff": 0.0002,
    "standx": 0.0002,
    "ethereal": 0.0002,
    "decibel": 0.0002,
    "treadfi.pacifica": 0.0003,
}
TAKER_FEE_RATES = {
    "hyperliquid": 0.00035,
    "miracle": 0.00035,
    "nado": 0.0005,
    "hotstuff": 0.0005,
    "standx": 0.0005,
    "ethereal": 0.0005,
    "decibel": 0.0005,
    "treadfi.pacifica": 0.0006,
}


@dataclass
class LimitOrderConfig:
    """지정가 주문 설정"""
    enabled: bool = False
    bbo_offset_percent: float = 0.01
    adjust_interval_ms: int = 100
    max_retries: int = 5
    pair_timeout_ms: int = 1000
    post_only: bool = True
    reprice_threshold: float = 0.02
    order_ttl_ms: int = 5000
    use_ioc_fallback: bool = True
    adaptive_offset: bool = True


@dataclass
class PairTraderV3Config:
    """V3 전략 설정"""
    coin1: str = "BTC"
    coin2: str = "ETH"
    leverage: int = 20
    trading_limit_count: int = 5
    trading_margin: float = 1000
    entry_trigger_percent: float = 0.2
    close_trigger_percent: float = 0.15
    stop_loss_percent: float = 5.0
    momentum_option: bool = True  # MA Crossover 종료 활성화
    stack_length: int = 5  # DCA 최대 스택
    chart_time: int = 15  # ETHBTC 캔들 프레임 (분)
    candle_limit: int = 100  # ETHBTC 캔들 조회 개수
    scan_interval: int = 60  # 스캔 간격 (초)
    min_momentum_diff: float = 0.0  # 최소 모멘텀 차이 (V3는 방향 결정에만 사용)
    min_candles: int = 200  # 최소 캔들 수
    ethbtc_interval: int = 15  # ETHBTC 캔들 간격 (분)
    ethbtc_limit: int = 100  # ETHBTC 캔들 조회 수
    consecutive_count: int = 5  # 연속 봉 수
    min_total_change_percent: float = 0.5  # 최소 총 변화율
    atr_filter: bool = True  # ATR 필터
    atr_threshold: float = 2.0  # ATR 임계값
    cooldown_candles: int = 1  # 쿨다운 캔들 수
    ma_crossover_stop: bool = True  # MA 크로스오버 종료
    ma_fast: int = 7  # 빠른 MA
    ma_slow: int = 20  # 느린 MA
    ma_min_gap_percent: float = 0.05  # MA 최소 갭
    fee_aware_auto_adjust: bool = True  # 수수료 인식 자동 조정
    limit_order: LimitOrderConfig = field(default_factory=LimitOrderConfig)


@dataclass
class Position:
    """포지션 기록"""
    coin: str
    side: str  # "long" or "short"
    entry_price: float = 0.0
    size: float = 0.0
    margin: float = 0.0
    entry_count: int = 0
    entries: list = field(default_factory=list)


class BinanceETHBTCFetcher:
    """Binance ETHBTC 스팟 캔들 데이터 조회"""

    BASE_URL = "https://api.binance.com/api/v3/klines"

    INTERVAL_MAP = {
        1: "1m",
        3: "3m",
        5: "5m",
        15: "15m",
        30: "30m",
        60: "1h",
        240: "4h",
        1440: "1d",
    }

    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy

    async def get_candles(
        self, symbol: str = "ETHBTC", interval: int = 15, limit: int = 100
    ) -> List[Dict]:
        """
        Binance ETHBTC 스팟 캔들 조회

        Parameters:
            symbol: 트레이딩 쌍 (기본값 "ETHBTC")
            interval: 캔들 타임프레임 (분)
            limit: 조회할 캔들 수

        Returns:
            list of dict with keys: timestamp, open, high, low, close, volume
        """
        tf = self.INTERVAL_MAP.get(interval, "15m")

        params = {
            "symbol": symbol.upper(),
            "interval": tf,
            "limit": min(limit, 1000),
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.BASE_URL, params=params, proxy=self.proxy, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"  Binance API 오류: {resp.status}")
                        return []

                    data = await resp.json()

            candles = []
            for c in data:
                candles.append({
                    "timestamp": c[0],
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[7]),  # Quote asset volume
                })

            return candles
        except Exception as e:
            logger.warning(f"  Binance ETHBTC 캔들 조회 실패: {e}")
            return []


def _to_order_side(side: str) -> str:
    """long/short → buy/sell 변환 (거래소 API 호환)"""
    s = side.lower()
    if s in ("long", "buy"):
        return "buy"
    if s in ("short", "sell"):
        return "sell"
    return s


class PairTraderV3:
    """V3 페어 트레이딩 엔진"""

    def __init__(
        self,
        exchange_wrapper,
        candle_fetcher,
        config: PairTraderV3Config,
        exchange_name: str = "",
    ):
        """
        Parameters:
            exchange_wrapper: MultiPerpDex 래퍼 인스턴스
            candle_fetcher: CandleFetcher 인스턴스 (coin1/coin2용)
            config: PairTraderV3Config
            exchange_name: 거래소 이름
        """
        self.wrapper = exchange_wrapper
        self.candle_fetcher = candle_fetcher
        self.config = config
        self.exchange_name = exchange_name
        self.running = False

        # ETHBTC 캔들 페처
        self.binance_fetcher = BinanceETHBTCFetcher()

        # 트레이드 로거
        self.trade_logger = None
        self._current_trade_id = None
        self._correlation_id: Optional[str] = None

        # 수수료 설정
        self._maker_fee_rate = MAKER_FEE_RATES.get(exchange_name, 0.0002)
        self._taker_fee_rate = TAKER_FEE_RATES.get(exchange_name, 0.0005)

        # 수수료 인식 진입/종료 트리거 (클로징 수수료 고려)
        round_trip_fee = (self._maker_fee_rate + self._taker_fee_rate) * 2 * 100 * 2.5
        self.min_close_trigger = round_trip_fee  # 최소 profitable close trigger
        self.min_entry_trigger = round_trip_fee  # 최소 profitable entry trigger

        # 설정의 close_trigger/entry_trigger가 충분히 높은지 확인
        if self.config.close_trigger_percent < self.min_close_trigger:
            logger.info(
                f"  {exchange_name} │ close_trigger {self.config.close_trigger_percent:.4f}% "
                f"→ {self.min_close_trigger:.4f}% (수수료 고려)"
            )
            self.config.close_trigger_percent = self.min_close_trigger
        if self.config.entry_trigger_percent < self.min_entry_trigger:
            logger.info(
                f"  {exchange_name} │ entry_trigger {self.config.entry_trigger_percent:.4f}% "
                f"→ {self.min_entry_trigger:.4f}% (수수료 고려)"
            )
            self.config.entry_trigger_percent = self.min_entry_trigger

        # 포지션 상태
        self.coin1_position: Optional[Position] = None
        self.coin2_position: Optional[Position] = None
        self.direction: Optional[str] = None  # "coin1_long" or "coin2_long"
        self.entry_count: int = 0

        # 모멘텀 점수
        self.coin1_momentum: float = 0.0
        self.coin2_momentum: float = 0.0

        # 캐시 데이터
        self._ethbtc_candles: List[Dict] = []
        self._coin1_candles: List[Dict] = []
        self._coin2_candles: List[Dict] = []

        # 진입/종료 기록 (DCA, ATR 필터링용)
        self._candle_entered: int = 0  # 진입한 캔들 인덱스
        self._last_entry_trigger_pnl: float = 0.0  # 마지막 entry trigger PnL
        self._last_first_entry_pnl: float = 0.0  # 최초 진입 PnL

        # DCA 레이스 컨디션 방지
        self._entry_lock = asyncio.Lock()

        # 짧은 표시 이름
        self.tag = exchange_name.upper()[:5].ljust(5)

        logger.info(
            f"  {self.tag} │ V3 초기화 │ {config.coin1}/{config.coin2} x{config.leverage} "
            f"close_trigger={self.config.close_trigger_percent:.4f}% "
            f"entry_trigger={self.config.entry_trigger_percent:.4f}%"
        )

    # ──────────────────────────────────────────
    # 공통 헬퍼
    # ──────────────────────────────────────────

    @staticmethod
    def _has_position(pos: dict) -> bool:
        """포지션 존재 여부"""
        return bool(pos and float(pos.get("size", 0)) != 0)

    def _safe_log_trade(self, method: str, **kwargs):
        """trade_logger 래퍼"""
        if not self.trade_logger:
            return None
        try:
            return getattr(self.trade_logger, method)(**kwargs)
        except Exception as e:
            logger.debug(f"  {self.tag} │ trade_logger.{method} 실패: {e}")
            return None

    async def _get_mark_price_safe(self, symbol: str, retries: int = 2) -> Optional[float]:
        """마크 가격 조회 (재시도)"""
        for attempt in range(retries):
            try:
                price = await asyncio.wait_for(
                    self.wrapper.get_mark_price(symbol), timeout=10.0
                )
                if price is not None and price > 0:
                    return price
            except asyncio.TimeoutError:
                logger.warning(f"  {self.tag} │ {symbol} 가격 조회 타임아웃 ({attempt+1}/{retries})")
            except Exception as e:
                logger.warning(f"  {self.tag} │ {symbol} 가격 조회 실패 ({attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(1)
        return None

    def set_logger(self, trade_logger):
        """트레이드 로거 주입"""
        self.trade_logger = trade_logger

    def get_state(self) -> dict:
        """트레이더 상태 직렬화"""
        if self.entry_count == 0 or not self.direction:
            return {}
        entries = []
        if self.coin1_position and self.coin1_position.entries:
            entries = list(self.coin1_position.entries)
        return {
            "exchange_name": self.exchange_name,
            "direction": self.direction,
            "entry_count": self.entry_count,
            "current_trade_id": self._current_trade_id,
            "entries": entries,
            "coin1": self.config.coin1,
            "coin2": self.config.coin2,
            "timestamp": datetime.now().isoformat(),
        }

    def restore_state(self, state: dict):
        """저장된 상태 복원"""
        self.direction = state.get("direction")
        self.entry_count = state.get("entry_count", 0)
        self._current_trade_id = state.get("current_trade_id")

        entries = state.get("entries", [])
        if entries and self.direction:
            is_coin1_long = self.direction == "coin1_long"
            self.coin1_position = Position(
                coin=self.config.coin1,
                side="long" if is_coin1_long else "short",
                entry_count=self.entry_count,
                entries=list(entries),
            )
            self.coin2_position = Position(
                coin=self.config.coin2,
                side="short" if is_coin1_long else "long",
                entry_count=self.entry_count,
                entries=list(entries),
            )

        logger.info(
            f"  {self.tag} │ 상태 복원 │ {self.direction} DCA={self.entry_count}"
        )

    # ──────────────────────────────────────────
    # V3 모멘텀 스코어 계산
    # ──────────────────────────────────────────

    def _calculate_v3_momentum_score(self, candles: List[Dict]) -> float:
        """
        V3 모멘텀 스코어 (5-factor, 범위: ~-100 to +100)

        ① Price ROC score (-30 to +30): clamp(roc50 * 0.6 + roc200 * 0.4, -30, 30)
        ② MA alignment (-25 to +25): current>ma20, ma20>ma50, ma50>ma100, ma100>ma200, price_vs_ma200
        ③ RSI score (-20 to +20): clamp(((rsi14-50)*0.6 + (rsi28-50)*0.4) * 0.4, -20, 20)
        ④ Volume ratio (~-15 to +15): ((bullVol - bearVol) / totalVol) * 15 from last 100
        ⑤ Consistency (~-10 to +10): (bullishCount/50 - 0.5) * 20 from last 50

        Parameters:
            candles: list of dict with keys: open, high, low, close, volume

        Returns:
            float: 모멘텀 점수 (-100~+100)
        """
        if len(candles) < 200:
            return 0.0

        closes = [c["close"] for c in candles]
        opens = [c["open"] for c in candles]
        volumes = [c["volume"] for c in candles]

        # ① Price ROC Score (-30 to +30)
        roc50 = ((closes[-1] - closes[-50]) / closes[-50]) * 100 if len(closes) >= 50 else 0
        roc200 = ((closes[-1] - closes[-200]) / closes[-200]) * 100 if len(closes) >= 200 else 0
        roc_score = max(-30, min(30, roc50 * 0.6 + roc200 * 0.4))

        # ② MA Alignment (-25 to +25)
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else 0
        ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else 0
        ma100 = sum(closes[-100:]) / 100 if len(closes) >= 100 else 0
        ma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else 0

        ma_align_score = 0
        if closes[-1] > ma20:
            ma_align_score += 5
        if ma20 > ma50:
            ma_align_score += 5
        if ma50 > ma100:
            ma_align_score += 5
        if ma100 > ma200:
            ma_align_score += 5
        if closes[-1] > ma200:
            ma_align_score += 5
        ma_align_score = (ma_align_score / 25) * 25 - 12.5  # 정규화: -12.5 to +12.5

        # ③ RSI Score (-20 to +20)
        def calc_rsi(prices, period=14):
            if len(prices) < period + 1:
                return 50.0
            gains, losses = [], []
            for i in range(-period, 0):
                diff = prices[i] - prices[i - 1]
                if diff > 0:
                    gains.append(diff)
                    losses.append(0)
                else:
                    gains.append(0)
                    losses.append(abs(diff))
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            if avg_loss == 0:
                return 100.0
            rs = avg_gain / avg_loss
            return 100 - (100 / (1 + rs))

        rsi14 = calc_rsi(closes, 14)
        rsi28 = calc_rsi(closes, 28)
        rsi_base = (rsi14 - 50) * 0.6 + (rsi28 - 50) * 0.4
        rsi_score = max(-20, min(20, rsi_base * 0.4))

        # ④ Volume Ratio (~-15 to +15) from last 100
        bull_vol = sum(
            v for i, v in enumerate(volumes[-100:]) if closes[len(closes) - 100 + i] > opens[len(closes) - 100 + i]
        )
        bear_vol = sum(
            v for i, v in enumerate(volumes[-100:]) if closes[len(closes) - 100 + i] <= opens[len(closes) - 100 + i]
        )
        total_vol = sum(volumes[-100:])
        if total_vol > 0:
            vol_ratio_score = ((bull_vol - bear_vol) / total_vol) * 15
        else:
            vol_ratio_score = 0

        # ⑤ Consistency (~-10 to +10) from last 50
        bullish_count = sum(1 for i in range(-50, 0) if closes[i] > closes[i - 1])
        consistency_score = (bullish_count / 50 - 0.5) * 20

        # 총 점수
        total_score = (
            roc_score + ma_align_score + rsi_score + vol_ratio_score + consistency_score
        )

        return max(-100, min(100, total_score))

    # ──────────────────────────────────────────
    # 연속 캔들 분석 (진입 신호)
    # ──────────────────────────────────────────

    def _analyze_consecutive_candles(self, candles: List[Dict]) -> Optional[str]:
        """
        N개 연속 캔들 분석 → 진입 신호 (config 파라미터 사용)

        - N개 연속 베어 캔들 + 합계 >= threshold → "long"
        - N개 연속 불 캔들 + 합계 >= threshold → "short"
        """
        n = self.config.consecutive_count
        threshold = self.config.min_total_change_percent

        if len(candles) < n + 1:
            return None

        recent = candles[-n:]

        # 베어 캔들 체크 (close < open만 — prev_close 조건 제거로 빈도 증가)
        is_bearish = all(c["close"] < c["open"] for c in recent)
        if is_bearish:
            total_change = ((recent[-1]["close"] - recent[0]["open"]) / recent[0]["open"]) * 100
            if total_change <= -threshold:
                return "long"

        # 불 캔들 체크
        is_bullish = all(c["close"] > c["open"] for c in recent)
        if is_bullish:
            total_change = ((recent[-1]["close"] - recent[0]["open"]) / recent[0]["open"]) * 100
            if total_change >= threshold:
                return "short"

        return None

    # ──────────────────────────────────────────
    # ATR 계산 및 필터
    # ──────────────────────────────────────────

    def _calculate_atr(self, candles: List[Dict], period: int = 14) -> float:
        """ATR 계산"""
        if len(candles) < period + 1:
            return 0.0

        trs = []
        for i in range(-period, 0):
            high = candles[i]["high"]
            low = candles[i]["low"]
            close_prev = candles[i - 1]["close"]

            tr = max(high - low, abs(high - close_prev), abs(low - close_prev))
            trs.append(tr)

        return sum(trs) / len(trs)

    def _check_atr_filter(self, candles: List[Dict]) -> bool:
        """
        ATR 필터: currentATR(14) / averageATR(40) < 2.0

        Returns:
            True if ATR is acceptable (not too volatile)
        """
        if len(candles) < 40 + 14:
            return True

        current_atr = self._calculate_atr(candles[-14:], 14)
        avg_atr = sum(self._calculate_atr(candles[i - 14 : i], 14) for i in range(-40, 0)) / 40

        if avg_atr == 0:
            return True

        ratio = current_atr / avg_atr
        return ratio < 2.0

    # ──────────────────────────────────────────
    # 진입 금액 계산
    # ──────────────────────────────────────────

    def _calculate_entry_amount(self, price: float) -> float:
        """
        진입 수량 계산

        margin_per_trade = trading_margin / trading_limit_count
        margin_per_coin = margin_per_trade / 2
        trading_value = margin_per_coin * leverage
        amount = trading_value / price
        """
        margin_per_trade = self.config.trading_margin / self.config.trading_limit_count
        margin_per_coin = margin_per_trade / 2
        trading_value = margin_per_coin * self.config.leverage
        amount = trading_value / price if price > 0 else 0
        return amount

    # ──────────────────────────────────────────
    # 포지션 진입
    # ──────────────────────────────────────────

    async def _execute_entry(self, price1: float, price2: float):
        """
        순차 진입: coin1 먼저, 그 다음 coin2

        Parameters:
            price1: coin1 마크 가격
            price2: coin2 마크 가격
        """
        async with self._entry_lock:
            if self.entry_count > 0:
                return  # 이미 진입함

            # 방향 결정: coin1 모멘텀 > coin2 → "coin1_long", otherwise "coin2_long"
            if self.coin1_momentum > self.coin2_momentum:
                direction = "coin1_long"
                coin1_side = "long"
                coin2_side = "short"
            else:
                direction = "coin2_long"
                coin1_side = "short"
                coin2_side = "long"

            try:
                # coin1 진입
                amount1 = self._calculate_entry_amount(price1)
                await self.wrapper.create_order(
                    self.config.coin1, _to_order_side(coin1_side), amount1, price=None, order_type="market"
                )
                self.coin1_position = Position(
                    coin=self.config.coin1,
                    side=coin1_side,
                    entry_price=price1,
                    size=amount1,
                    entry_count=1,
                    entries=[{"price": price1, "size": amount1, "time": time.time()}],
                )
                logger.info(
                    f"  {self.tag} │ 진입1 │ {self.config.coin1} {coin1_side.upper()} "
                    f"{amount1:.6f} @ {price1:.2f}"
                )

                await asyncio.sleep(0.5)  # 순차 진입 딜레이

                # coin2 진입
                amount2 = self._calculate_entry_amount(price2)
                await self.wrapper.create_order(
                    self.config.coin2, _to_order_side(coin2_side), amount2, price=None, order_type="market"
                )
                self.coin2_position = Position(
                    coin=self.config.coin2,
                    side=coin2_side,
                    entry_price=price2,
                    size=amount2,
                    entry_count=1,
                    entries=[{"price": price2, "size": amount2, "time": time.time()}],
                )
                logger.info(
                    f"  {self.tag} │ 진입2 │ {self.config.coin2} {coin2_side.upper()} "
                    f"{amount2:.6f} @ {price2:.2f}"
                )

                self.direction = direction
                self.entry_count = 1
                self._candle_entered = len(self._ethbtc_candles) - 1
                self._current_trade_id = str(uuid.uuid4())
                self._correlation_id = str(uuid.uuid4())

                self._safe_log_trade(
                    "open_trade",
                    trade_id=self._current_trade_id,
                    direction=direction,
                    coin1=self.config.coin1,
                    coin2=self.config.coin2,
                    leverage=self.config.leverage,
                )

            except Exception as e:
                logger.error(f"  {self.tag} │ 진입 실패: {e}")

    # ──────────────────────────────────────────
    # DCA 진입 조건 확인
    # ──────────────────────────────────────────

    def _check_add_position_condition(self, spread_pnl: float) -> bool:
        """
        DCA 추가 진입 조건 (이중 조건)

        1. spreadPnlFromLast <= -entry_trigger_percent
        2. spreadPnlFromFirst <= -entry_trigger_percent * stack_length

        Parameters:
            spread_pnl: 현재 스프레드 PnL (%)

        Returns:
            True if both conditions met
        """
        if not self.coin1_position or not self.coin2_position:
            return False

        # Last entry PnL (마지막 진입 이후 변화)
        spread_pnl_from_last = spread_pnl - self._last_entry_trigger_pnl

        # First entry PnL (첫 진입 이후 변화)
        spread_pnl_from_first = spread_pnl - self._last_first_entry_pnl

        condition1 = spread_pnl_from_last <= -self.config.entry_trigger_percent
        condition2 = spread_pnl_from_first <= -self.config.entry_trigger_percent * self.config.stack_length

        return condition1 and condition2

    async def _execute_add_position(self, price1: float, price2: float):
        """DCA 추가 진입"""
        async with self._entry_lock:
            if self.entry_count >= self.config.trading_limit_count:
                return

            try:
                is_coin1_long = self.direction == "coin1_long"
                coin1_side = "long" if is_coin1_long else "short"
                coin2_side = "short" if is_coin1_long else "long"

                # coin1 추가 진입
                amount1 = self._calculate_entry_amount(price1)
                await self.wrapper.create_order(
                    self.config.coin1, _to_order_side(coin1_side), amount1, price=None, order_type="market"
                )
                self.coin1_position.size += amount1
                self.coin1_position.entries.append(
                    {"price": price1, "size": amount1, "time": time.time()}
                )
                logger.info(
                    f"  {self.tag} │ DCA1 │ {self.config.coin1} {coin1_side.upper()} "
                    f"{amount1:.6f} @ {price1:.2f}"
                )

                await asyncio.sleep(0.3)

                # coin2 추가 진입
                amount2 = self._calculate_entry_amount(price2)
                await self.wrapper.create_order(
                    self.config.coin2, _to_order_side(coin2_side), amount2, price=None, order_type="market"
                )
                self.coin2_position.size += amount2
                self.coin2_position.entries.append(
                    {"price": price2, "size": amount2, "time": time.time()}
                )
                logger.info(
                    f"  {self.tag} │ DCA2 │ {self.config.coin2} {coin2_side.upper()} "
                    f"{amount2:.6f} @ {price2:.2f}"
                )

                self.entry_count += 1
                self._last_entry_trigger_pnl = self._calculate_total_pnl_percent(price1, price2)

            except Exception as e:
                logger.error(f"  {self.tag} │ DCA 실패: {e}")

    # ──────────────────────────────────────────
    # PnL 계산
    # ──────────────────────────────────────────

    def _calculate_total_pnl_percent(self, price1: float, price2: float) -> float:
        """
        총 스프레드 PnL 계산 (%)

        각 코인: if long → (current - entry) / entry * 100
                if short → (entry - current) / entry * 100

        스프레드 PnL = coin1PnL + coin2PnL
        """
        if not self.coin1_position or not self.coin2_position:
            return 0.0

        is_coin1_long = self.direction == "coin1_long"

        # coin1 PnL
        if is_coin1_long:
            coin1_pnl = ((price1 - self.coin1_position.entry_price) / self.coin1_position.entry_price) * 100
        else:
            coin1_pnl = ((self.coin1_position.entry_price - price1) / self.coin1_position.entry_price) * 100

        # coin2 PnL
        if is_coin1_long:
            coin2_pnl = ((self.coin2_position.entry_price - price2) / self.coin2_position.entry_price) * 100
        else:
            coin2_pnl = ((price2 - self.coin2_position.entry_price) / self.coin2_position.entry_price) * 100

        return coin1_pnl + coin2_pnl

    # ──────────────────────────────────────────
    # MA Crossover 종료 조건
    # ──────────────────────────────────────────

    def _check_ma_crossover_stop(self) -> bool:
        """
        MA Crossover 종료 (momentum_option=true일 때)

        MA(7) / MA(20) ETHBTC with min gap 0.05%
        - coin1 LONG & golden cross 감지 → close
        - coin1 SHORT & dead cross 감지 → close

        Returns:
            True if crossover detected (should close)
        """
        if not self.config.momentum_option or len(self._ethbtc_candles) < 21:
            return False

        closes = [c["close"] for c in self._ethbtc_candles]
        ma7 = sum(closes[-7:]) / 7
        ma20 = sum(closes[-20:]) / 20

        ma7_prev = sum(closes[-8:-1]) / 7
        ma20_prev = sum(closes[-21:-1]) / 20

        is_coin1_long = self.direction == "coin1_long"

        # Golden cross (MA7 상향 돌파)
        if is_coin1_long:
            gap = ((ma7 - ma20) / ma20) * 100
            gap_prev = ((ma7_prev - ma20_prev) / ma20_prev) * 100
            if gap_prev < -0.05 and gap >= -0.05:  # 상향 돌파
                logger.info(f"  {self.tag} │ MA Cross Stop │ Golden Cross 감지 (coin1 LONG)")
                return True

        # Dead cross (MA7 하향 돌파)
        else:
            gap = ((ma7 - ma20) / ma20) * 100
            gap_prev = ((ma7_prev - ma20_prev) / ma20_prev) * 100
            if gap_prev > 0.05 and gap <= 0.05:  # 하향 돌파
                logger.info(f"  {self.tag} │ MA Cross Stop │ Dead Cross 감지 (coin1 SHORT)")
                return True

        return False

    # ──────────────────────────────────────────
    # 포지션 청산
    # ──────────────────────────────────────────

    async def _execute_close(self, reason: str, price1: float = None, price2: float = None):
        """
        포지션 청산

        Parameters:
            reason: 청산 이유 ("take_profit", "stop_loss", "ma_crossover", ...)
            price1, price2: 청산 시점 가격 (PnL 계산용)
        """
        try:
            if not self.coin1_position or not self.coin2_position:
                return

            # 현재 가격 조회 (price가 없을 경우)
            if price1 is None:
                price1 = await self._get_mark_price_safe(self.config.coin1)
            if price2 is None:
                price2 = await self._get_mark_price_safe(self.config.coin2)

            if not price1 or not price2:
                logger.warning(f"  {self.tag} │ 청산 가격 조회 실패")
                return

            # PnL 계산
            total_pnl = self._calculate_total_pnl_percent(price1, price2)

            # coin1 청산
            pos1 = await self.wrapper.get_position(self.config.coin1)
            if self._has_position(pos1):
                await self.wrapper.close_position(self.config.coin1, pos1)
                logger.info(
                    f"  {self.tag} │ 청산1 │ {self.config.coin1} {pos1['side'].upper()} "
                    f"{pos1['size']:.6f} @ {price1:.2f}"
                )

            await asyncio.sleep(0.3)

            # coin2 청산
            pos2 = await self.wrapper.get_position(self.config.coin2)
            if self._has_position(pos2):
                await self.wrapper.close_position(self.config.coin2, pos2)
                logger.info(
                    f"  {self.tag} │ 청산2 │ {self.config.coin2} {pos2['side'].upper()} "
                    f"{pos2['size']:.6f} @ {price2:.2f}"
                )

            logger.info(
                f"  {self.tag} │ 청산 │ {reason} │ PnL {total_pnl:.2f}%"
            )

            self._safe_log_trade(
                "close_trade",
                trade_id=self._current_trade_id,
                pnl_percent=total_pnl,
                pnl_usd=0.0,  # TODO: Calculate actual USD PnL
                reason=reason,
            )

            # 상태 초기화
            self.coin1_position = None
            self.coin2_position = None
            self.direction = None
            self.entry_count = 0
            self._current_trade_id = None

        except Exception as e:
            logger.error(f"  {self.tag} │ 청산 실패: {e}")

    # ──────────────────────────────────────────
    # 데이터 업데이트
    # ──────────────────────────────────────────

    async def _update_data(self):
        """
        캔들 데이터 업데이트

        - ETHBTC (Binance): 15분봉 100개
        - coin1/coin2: 5분봉 1000개 (기존 CandleFetcher)
        """
        # ETHBTC 데이터 (Binance) — 타임아웃 15초
        try:
            ethbtc_candles = await asyncio.wait_for(
                self.binance_fetcher.get_candles(
                    "ETHBTC", interval=self.config.chart_time, limit=self.config.candle_limit
                ), timeout=15.0
            )
            if ethbtc_candles:
                self._ethbtc_candles = ethbtc_candles
            else:
                logger.warning(f"  {self.tag} │ ETHBTC 캔들 조회 실패")
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"  {self.tag} │ ETHBTC 캔들 에러: {e}")

        # coin1/coin2 데이터 (candle_fetcher가 있을 때만)
        if self.candle_fetcher:
            for coin_attr, coin_name in [("_coin1_candles", self.config.coin1), ("_coin2_candles", self.config.coin2)]:
                try:
                    candles = await asyncio.wait_for(
                        self.candle_fetcher.get_candles(coin_name, interval=5, limit=1000),
                        timeout=10.0,
                    )
                    if candles:
                        setattr(self, coin_attr, candles)
                except Exception as e:
                    logger.debug(f"  {self.tag} │ {coin_name} 캔들 조회 실패: {e}")

    async def _update_momentum(self):
        """모멘텀 스코어 업데이트"""
        if len(self._coin1_candles) >= 200:
            self.coin1_momentum = self._calculate_v3_momentum_score(self._coin1_candles)
        else:
            self.coin1_momentum = 0.0

        if len(self._coin2_candles) >= 200:
            self.coin2_momentum = self._calculate_v3_momentum_score(self._coin2_candles)
        else:
            self.coin2_momentum = 0.0

    # ──────────────────────────────────────────
    # 메인 트레이딩 사이클
    # ──────────────────────────────────────────

    async def _trade_cycle(self):
        """V3 트레이딩 메인 사이클"""
        # 1. 데이터 업데이트
        logger.debug(f"  {self.tag} │ 사이클 시작: 데이터 업데이트")
        try:
            await asyncio.wait_for(self._update_data(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(f"  {self.tag} │ 데이터 업데이트 타임아웃 30초")
            return

        # 2. 모멘텀 계산
        await self._update_momentum()

        # 3. 현재 가격 조회
        price1 = await self._get_mark_price_safe(self.config.coin1)
        price2 = await self._get_mark_price_safe(self.config.coin2)

        if not price1 or not price2 or price1 <= 0 or price2 <= 0:
            logger.warning(
                f"  {self.tag} │ 가격 이상: {self.config.coin1}={price1}, "
                f"{self.config.coin2}={price2} │ 사이클 스킵"
            )
            return

        # 4. 포지션 없을 때 → 진입 분석
        if self.entry_count == 0:
            if not hasattr(self, '_v3_cycle_count'):
                self._v3_cycle_count = 0
            self._v3_cycle_count += 1
            if self._v3_cycle_count % 10 == 1:
                logger.info(
                    f"  {self.tag} │ V3 대기 │ ETHBTC봉={len(self._ethbtc_candles)} "
                    f"{self.config.coin1}={price1:.0f} {self.config.coin2}={price2:.2f}"
                )

            # 연속 캔들 분석
            entry_signal = self._analyze_consecutive_candles(self._ethbtc_candles)

            if entry_signal:
                # ATR 필터 확인
                if not self._check_atr_filter(self._ethbtc_candles):
                    logger.info(f"  {self.tag} │ 진입 신호 차단 │ ATR 필터 (변동성 과도)")
                    return

                logger.info(
                    f"  {self.tag} │ 진입 신호 │ {entry_signal.upper()} │ "
                    f"momentum: {self.config.coin1}={self.coin1_momentum:.1f} "
                    f"{self.config.coin2}={self.coin2_momentum:.1f}"
                )

                await self._execute_entry(price1, price2)

                # 첫 진입 PnL 기록
                self._last_first_entry_pnl = self._calculate_total_pnl_percent(price1, price2)
                self._last_entry_trigger_pnl = self._last_first_entry_pnl

        # 5. 포지션 있을 때 → 청산 또는 DCA
        else:
            total_pnl = self._calculate_total_pnl_percent(price1, price2)

            # Take Profit 확인
            if total_pnl >= self.config.close_trigger_percent:
                logger.info(
                    f"  {self.tag} │ Take Profit │ PnL {total_pnl:.2f}% >= "
                    f"{self.config.close_trigger_percent:.2f}%"
                )
                await self._execute_close("take_profit", price1, price2)
                return

            # Stop Loss 확인
            if total_pnl <= -self.config.stop_loss_percent:
                logger.info(
                    f"  {self.tag} │ Stop Loss │ PnL {total_pnl:.2f}% <= "
                    f"-{self.config.stop_loss_percent:.2f}%"
                )
                await self._execute_close("stop_loss", price1, price2)
                return

            # MA Crossover 청산
            if self._check_ma_crossover_stop():
                await self._execute_close("ma_crossover", price1, price2)
                return

            # DCA 추가 진입
            if self._check_add_position_condition(total_pnl):
                if self.entry_count < self.config.trading_limit_count:
                    logger.info(
                        f"  {self.tag} │ DCA 신호 │ PnL {total_pnl:.2f}% "
                        f"({self.entry_count}/{self.config.trading_limit_count})"
                    )
                    await self._execute_add_position(price1, price2)

    # ──────────────────────────────────────────
    # 메인 루프
    # ──────────────────────────────────────────

    async def run(self, saved_state: dict = None):
        """메인 루프"""
        self.running = True
        logger.info(f"  {self.tag} │ V3 트레이딩 시작")

        # 저장된 상태 복원 (타임아웃 60초)
        try:
            await asyncio.wait_for(self._init_positions(saved_state), timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning(f"  {self.tag} │ 초기화 타임아웃 60초 — 새로 시작")

        # 레버리지 설정
        try:
            await asyncio.wait_for(
                self.wrapper.update_leverage(self.config.coin1, leverage=self.config.leverage),
                timeout=15.0,
            )
            await asyncio.wait_for(
                self.wrapper.update_leverage(self.config.coin2, leverage=self.config.leverage),
                timeout=15.0,
            )
            logger.info(f"  {self.tag} │ 레버리지 {self.config.leverage}x 설정")
        except Exception as e:
            logger.warning(f"  {self.tag} │ 레버리지 설정 실패: {e}")

        # 메인 루프
        logger.info(f"  {self.tag} │ V3 메인 루프 진입")
        cycle = 0
        while self.running:
            try:
                cycle += 1
                if cycle <= 3 or cycle % 30 == 0:
                    logger.info(f"  {self.tag} │ V3 사이클 #{cycle}")
                await self._trade_cycle()
                await asyncio.sleep(self.config.scan_interval)
            except Exception as e:
                logger.error(f"  {self.tag} │ 에러: {e}")
                await asyncio.sleep(self.config.scan_interval)

    def stop(self):
        """트레이딩 중지"""
        self.running = False
        logger.info(f"  {self.tag} │ V3 트레이딩 중지")

    async def shutdown(self, close_positions: bool = True):
        """봇 종료"""
        self.running = False
        if close_positions:
            logger.info(f"  {self.tag} │ V3 종료 중... 포지션 정리")
            await self._cleanup_existing_positions()
        else:
            logger.info(f"  {self.tag} │ V3 종료 중... 포지션 유지")
        logger.info(f"  {self.tag} │ V3 종료 완료")

    async def _init_positions(self, saved_state=None):
        """포지션 초기화 (복원 또는 청산)"""
        if saved_state:
            pos1 = await asyncio.wait_for(self.wrapper.get_position(self.config.coin1), timeout=15.0)
            pos2 = await asyncio.wait_for(self.wrapper.get_position(self.config.coin2), timeout=15.0)
            has_pos = self._has_position(pos1) or self._has_position(pos2)
            if has_pos:
                self.restore_state(saved_state)
                return
            logger.info(f"  {self.tag} │ 저장 상태 있으나 포지션 없음 → 새로 시작")
        await self._cleanup_existing_positions()

    async def _cleanup_existing_positions(self):
        """기존 포지션 청산"""
        try:
            coins_to_check = [self.config.coin1, self.config.coin2]
            for coin in coins_to_check:
                try:
                    pos = await asyncio.wait_for(
                        self.wrapper.get_position(coin), timeout=15.0
                    )
                    if self._has_position(pos):
                        await asyncio.wait_for(
                            self.wrapper.close_position(coin, pos), timeout=15.0
                        )
                        logger.info(f"  {self.tag} │ {coin} 기존 포지션 청산")
                except (asyncio.TimeoutError, Exception) as e:
                    logger.debug(f"  {self.tag} │ {coin} 포지션 청산 스킵: {e}")

            self.coin1_position = None
            self.coin2_position = None
            self.direction = None
            self.entry_count = 0
        except Exception as e:
            logger.error(f"  {self.tag} │ 포지션 확인 실패: {e}")
