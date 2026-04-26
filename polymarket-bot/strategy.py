"""Signal generation: RSI, Bollinger Bands, VWAP, momentum scoring."""

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np
import websockets

from config import Config


@dataclass
class Tick:
    price: float
    volume: float
    timestamp: float


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap_numerator: float
    timestamp: float


@dataclass
class SignalOutput:
    direction_bias: float
    model_prob_up: float
    rsi: float
    bb_upper: float
    bb_lower: float
    bb_mid: float
    vwap: float
    current_price: float
    momentum: float
    trend_strength: float
    vol_regime: str = "medium"
    bb_width_pct: float = 0.0
    htf_trend: float = 0.0


class PriceEngine:
    """Manages real-time Binance trades and signal computation for one asset."""

    def __init__(self, config: Config, asset_symbol: str = "BTC"):
        self.config = config
        self.asset_symbol = asset_symbol.upper()
        self.binance_symbol = f"{self.asset_symbol}USDT"
        self.ws_url = f"wss://stream.binance.com:9443/ws/{self.binance_symbol.lower()}@trade"
        self.ticks: deque[Tick] = deque(maxlen=config.tick_cache_size)
        self.candles: list[Candle] = []
        self.current_candle: Optional[Candle] = None
        self.current_candle_minute: int = -1
        self._running = False
        self._latest_signal: Optional[SignalOutput] = None
        self._ws = None
        # Higher-timeframe candles for multi-timeframe analysis
        self.htf_candles: list[Candle] = []
        self._htf_fetched = False
        # Strike price cache: window_start_ts -> price
        self._strike_cache: dict[int, float] = {}

    @property
    def latest_price(self) -> float:
        return self.ticks[-1].price if self.ticks else 0.0

    @property
    def latest_signal(self) -> Optional[SignalOutput]:
        return self._latest_signal

    async def _prefetch_candles(self):
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{self.config.binance_rest_url}/api/v3/klines",
                    params={"symbol": self.binance_symbol, "interval": "1m", "limit": self.config.candle_history},
                )
                resp.raise_for_status()
                for k in resp.json():
                    self.candles.append(Candle(
                        open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4]),
                        volume=float(k[5]), vwap_numerator=float(k[4]) * float(k[5]), timestamp=float(k[0]) / 1000.0,
                    ))
                if self.candles:
                    self.current_candle_minute = int(self.candles[-1].timestamp // 60)
                    print(f"[PriceEngine:{self.asset_symbol}] Prefetched {len(self.candles)} candles")
                # Fetch higher-timeframe candles
                if self.config.htf_enabled:
                    htf_resp = await client.get(
                        f"{self.config.binance_rest_url}/api/v3/klines",
                        params={"symbol": self.binance_symbol, "interval": self.config.htf_candle_interval, "limit": self.config.htf_candle_count},
                    )
                    htf_resp.raise_for_status()
                    for k in htf_resp.json():
                        self.htf_candles.append(Candle(
                            open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4]),
                            volume=float(k[5]), vwap_numerator=float(k[4]) * float(k[5]), timestamp=float(k[0]) / 1000.0,
                        ))
                    self._htf_fetched = True
                    print(f"[PriceEngine:{self.asset_symbol}] Prefetched {len(self.htf_candles)} HTF candles ({self.config.htf_candle_interval})")
        except Exception as e:
            print(f"[PriceEngine:{self.asset_symbol}] Prefetch failed: {e}")

    async def start(self):
        self._running = True
        await self._prefetch_candles()
        self.compute_signals()
        while self._running:
            try:
                ws = await websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10, close_timeout=5)
                self._ws = ws
                try:
                    while self._running:
                        data = json.loads(await ws.recv())
                        tick = Tick(price=float(data["p"]), volume=float(data["q"]), timestamp=float(data["T"]) / 1000.0)
                        self.ticks.append(tick)
                        self._update_candle(tick)
                finally:
                    await ws.close()
            except (websockets.ConnectionClosed, ConnectionError, OSError, Exception) as e:
                if self._running:
                    print(f"[PriceEngine:{self.asset_symbol}] WS disconnected: {e}, reconnecting...")
                    await asyncio.sleep(2)

    def stop(self):
        self._running = False

    def _update_candle(self, tick: Tick):
        minute = int(tick.timestamp // 60)
        if minute != self.current_candle_minute:
            if self.current_candle is not None:
                self.candles.append(self.current_candle)
                if len(self.candles) > self.config.candle_history:
                    self.candles = self.candles[-self.config.candle_history:]
            self.current_candle = Candle(
                open=tick.price, high=tick.price, low=tick.price, close=tick.price,
                volume=tick.volume, vwap_numerator=tick.price * tick.volume, timestamp=tick.timestamp,
            )
            self.current_candle_minute = minute
        elif self.current_candle is not None:
            candle = self.current_candle
            candle.high = max(candle.high, tick.price)
            candle.low = min(candle.low, tick.price)
            candle.close = tick.price
            candle.volume += tick.volume
            candle.vwap_numerator += tick.price * tick.volume

    def compute_signals(self) -> Optional[SignalOutput]:
        all_candles = list(self.candles)
        if self.current_candle is not None:
            all_candles.append(self.current_candle)
        if len(all_candles) < max(self.config.rsi_period + 1, self.config.bb_period):
            return None

        closes = np.array([c.close for c in all_candles])
        volumes = np.array([c.volume for c in all_candles])
        rsi = self._calc_rsi(closes, self.config.rsi_period)
        bb_upper, bb_mid, bb_lower = self._calc_bollinger(closes, self.config.bb_period, self.config.bb_std)
        vwap = self._calc_vwap(all_candles)
        current_price = closes[-1]
        momentum = self._calc_momentum(current_price, vwap, rsi)
        trend_strength = self._calc_trend_strength(closes)  # default lookback=20
        short_trend = self._calc_trend_strength(closes, lookback=5)
        mid_trend = self._calc_trend_strength(closes, lookback=10)
        long_trend = trend_strength  # same lookback=20, reuse
        recent_vol = np.mean(volumes[-5:]) if len(volumes) >= 5 else 1.0
        avg_vol = np.mean(volumes[-20:]) if len(volumes) >= 20 else recent_vol
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
        vol_boost = min(vol_ratio, 2.5)

        # Bollinger band position: strong directional signal
        bb_width = bb_upper - bb_lower
        if bb_width > 0:
            bb_position = (current_price - bb_mid) / (bb_width / 2.0)
            bb_signal = np.clip(bb_position, -1.0, 1.0)
        else:
            bb_signal = 0.0

        # RSI extremes give stronger directional confidence
        if rsi >= 70:
            rsi_extreme = min((rsi - 70) / 20.0, 1.0)  # overbought → UP momentum
        elif rsi <= 30:
            rsi_extreme = -min((30 - rsi) / 20.0, 1.0)  # oversold → DOWN momentum
        else:
            rsi_extreme = 0.0

        trend_agreement = 1.0
        if (short_trend > 0 and long_trend < 0) or (short_trend < 0 and long_trend > 0):
            trend_agreement = 0.5  # less aggressive penalty for disagreement

        # Combine signals with wider range
        raw_bias = (
            momentum * 0.25
            + short_trend * 0.25
            + mid_trend * 0.15
            + long_trend * 0.05
            + bb_signal * 0.15
            + rsi_extreme * 0.15
        ) * trend_agreement
        direction_bias = np.clip(raw_bias, -1.0, 1.0)

        # Volatility regime detection
        bb_width_pct = bb_width / bb_mid if bb_mid > 0 else 0.0
        if bb_width_pct < self.config.vol_regime_low_threshold:
            vol_regime = "low"
        elif bb_width_pct > self.config.vol_regime_high_threshold:
            vol_regime = "high"
        else:
            vol_regime = "medium"

        # Higher-timeframe trend confirmation
        htf_trend = 0.0
        if self.htf_candles and self.config.htf_enabled:
            htf_closes = np.array([c.close for c in self.htf_candles[-15:]])
            if len(htf_closes) >= 5:
                htf_trend = self._calc_trend_strength(htf_closes, lookback=min(10, len(htf_closes)))
                # If HTF agrees with short-term, boost confidence
                if htf_trend * short_trend > 0:
                    direction_bias = np.clip(direction_bias * 1.2, -1.0, 1.0)
                # If HTF disagrees, dampen
                elif htf_trend * short_trend < 0:
                    direction_bias *= 0.7

        # Wider probability range: 0.5 ± 0.40 max
        confidence_boost = min(vol_boost, 2.0)
        # Low vol → more confident in direction, high vol → less confident
        vol_confidence = 1.2 if vol_regime == "low" else (0.8 if vol_regime == "high" else 1.0)
        model_prob_up = 0.5 + direction_bias * (0.40 * confidence_boost / 2.0) * vol_confidence
        model_prob_up = np.clip(model_prob_up, 0.05, 0.95)
        signal = SignalOutput(
            direction_bias=direction_bias,
            model_prob_up=model_prob_up,
            rsi=rsi,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            bb_mid=bb_mid,
            vwap=vwap,
            current_price=current_price,
            momentum=momentum,
            trend_strength=trend_strength,
            vol_regime=vol_regime,
            bb_width_pct=bb_width_pct,
            htf_trend=htf_trend,
        )
        self._latest_signal = signal
        return signal

    @staticmethod
    def _calc_rsi(closes: np.ndarray, period: int) -> float:
        if len(closes) < 2:
            return 50.0
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_gain == 0 and avg_loss == 0:
            return 50.0  # no movement = neutral
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _calc_bollinger(closes: np.ndarray, period: int, num_std: float):
        if len(closes) == 0:
            return 0.0, 0.0, 0.0
        if len(closes) < period:
            mid = closes[-1]
            return mid, mid, mid
        window = closes[-period:]
        mid = np.mean(window)
        std = np.std(window)
        upper = mid + num_std * std
        lower = mid - num_std * std
        return upper, mid, lower

    @staticmethod
    def _calc_vwap(candles: list[Candle]) -> float:
        total_pv = sum(c.vwap_numerator for c in candles)
        total_v = sum(c.volume for c in candles)
        return total_pv / total_v if total_v else (candles[-1].close if candles else 0.0)

    @staticmethod
    def _calc_momentum(price: float, vwap: float, rsi: float) -> float:
        price_component = 0.0 if vwap == 0 else np.clip(((price - vwap) / vwap) * 200, -1.0, 1.0)
        rsi_component = (rsi - 50.0) / 50.0
        return np.clip(price_component * 0.6 + rsi_component * 0.4, -1.0, 1.0)

    @staticmethod
    def _calc_trend_strength(closes: np.ndarray, lookback: int = 20) -> float:
        if len(closes) < lookback:
            lookback = len(closes)
        window = closes[-lookback:]
        x = np.arange(len(window))
        if len(window) < 2:
            return 0.0
        slope = np.polyfit(x, window, 1)[0]
        normalized = slope / window[-1] * 100
        return np.clip(normalized, -1.0, 1.0)

    async def fetch_strike_price(self, window_start_ts: float) -> Optional[float]:
        """Fetch the asset price at a specific timestamp from Binance."""
        cache_key = int(window_start_ts // 60)
        if cache_key in self._strike_cache:
            return self._strike_cache[cache_key]
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self.config.binance_rest_url}/api/v3/klines",
                    params={
                        "symbol": self.binance_symbol,
                        "interval": "1m",
                        "startTime": int(window_start_ts * 1000),
                        "limit": 1,
                    },
                )
                resp.raise_for_status()
                klines = resp.json()
                if klines:
                    price = float(klines[0][1])  # open price at window start
                    self._strike_cache[cache_key] = price
                    # Keep cache small
                    if len(self._strike_cache) > 100:
                        oldest = sorted(self._strike_cache.keys())[:50]
                        for k in oldest:
                            self._strike_cache.pop(k, None)
                    return price
        except Exception:
            pass
        return None

    def calc_strike_prob(self, current_price: float, strike_price: float, minutes_to_expiry: float, duration_minutes: float) -> float:
        """Calculate probability that price stays above/below strike at expiry.

        Uses distance from strike normalized by recent volatility.
        Closer to expiry + larger distance = more certain.
        """
        if strike_price <= 0 or current_price <= 0:
            return 0.5
        pct_diff = (current_price - strike_price) / strike_price
        # Time factor: closer to expiry → less time for reversal
        time_elapsed_ratio = max(0.0, 1.0 - (minutes_to_expiry / max(1.0, duration_minutes)))
        # Volatility context: use BB width as proxy
        signal = self._latest_signal
        vol_factor = 1.0
        if signal and signal.bb_width_pct > 0:
            vol_factor = max(0.3, min(2.0, 0.005 / signal.bb_width_pct))
        # Combine: larger move + more time elapsed + lower vol = more confident
        confidence = abs(pct_diff) * 500.0 * (0.5 + time_elapsed_ratio * 0.5) * vol_factor
        confidence = min(confidence, 0.45)  # cap at ±0.45 from 0.5
        if pct_diff > 0:
            return 0.5 + confidence  # price above strike → UP more likely
        else:
            return 0.5 - confidence  # price below strike → DOWN more likely

    def get_signal_dict(self) -> dict:
        signal = self._latest_signal
        if signal is None:
            return {}
        return {
            "asset_symbol": self.asset_symbol,
            "price": round(signal.current_price, 2),
            "rsi": round(signal.rsi, 2),
            "bb_upper": round(signal.bb_upper, 2),
            "bb_lower": round(signal.bb_lower, 2),
            "bb_mid": round(signal.bb_mid, 2),
            "vwap": round(signal.vwap, 2),
            "momentum": round(signal.momentum, 4),
            "trend_strength": round(signal.trend_strength, 4),
            "direction_bias": round(signal.direction_bias, 4),
            "model_prob_up": round(signal.model_prob_up, 4),
            "vol_regime": signal.vol_regime,
            "bb_width_pct": round(signal.bb_width_pct, 6),
            "htf_trend": round(signal.htf_trend, 4),
        }
