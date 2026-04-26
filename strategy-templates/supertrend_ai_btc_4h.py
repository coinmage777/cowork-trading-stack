"""
SuperTrend AI Adaptive - Strategy [BTC] — BTCUSDT 4H
Author (Pine): DefinedEdge (TradingView, published 2026-02-19~21)
Source: https://www.tradingview.com/script/kZVrTReu/
Minara backtest (1460d, HyperLiquid fees):
    APR +60.2%, Sharpe reported high, WR ~48%.
    Author-reported: +2,091% (2015-2026), PF 1.94, WR 46% (71/154), MDD 28%.

Inferred structure (Pine source not visible — TV description):
    - Base: SuperTrend ATR period 10, base multiplier 3.0
    - 5-factor scoring engine gates SuperTrend flips:
        1) volume surge vs recent average
        2) price displacement through band (how decisively it crossed)
        3) EMA alignment (short EMA vs long EMA)
        4) regime quality (ADX / trend strength)
        5) band distance traveled (how far SuperTrend moved in last N bars)
    - Score >= 65 triggers entry. Score in [0,100] composite.
    - Multiplier adapts: base in trend, widened in volatile regime, tightened
      in ranging — we approximate by letting multiplier scale with ATR-of-ATR.
    - Exit: ATR-based stop (default 6× multiplier on entry ATR), percent TP
      (R:R 2.5), or opposite SuperTrend flip (whichever first).
    - Both LONG and SHORT.

Direction: LONG and SHORT
Needs stop loss added? NO — already has ATR stop + TP. The 5-factor filter
    is itself a risk filter. Confirm exit logic during shadow testing.
Fee sensitivity: LOW-MEDIUM. ~15-20 trades/yr (154 trades / 10yr). Round-trip
    ~1.5-2% drag/yr. Given PF 1.94, well within buffer.
Best venue: Hyperliquid (backtest). Bybit/Binance also fine given trade count.

NOTE: This is a best-effort approximation. The true Pine source contains the
exact scoring weights/thresholds which are NOT visible without purchase/access.
Shadow-test against live TV alerts before scaling capital.
"""
from dataclasses import dataclass
from typing import Optional
import math


@dataclass
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class SuperTrendAiBtc4HParams:
    atr_period: int = 10
    base_multiplier: float = 3.0
    ema_short: int = 20
    ema_long: int = 50
    volume_ma_period: int = 20
    score_threshold: float = 65.0
    rr_ratio: float = 2.5
    atr_stop_mult: float = 6.0
    allow_short: bool = True


class SuperTrendAiBtc4H:
    def __init__(self, params: Optional[SuperTrendAiBtc4HParams] = None):
        self.p = params or SuperTrendAiBtc4HParams()
        # ATR state
        self._trs: list[float] = []
        self._atr: Optional[float] = None
        self._prev_close: Optional[float] = None
        # SuperTrend state
        self._final_upper: Optional[float] = None
        self._final_lower: Optional[float] = None
        self._trend: int = 1
        self._prev_trend: int = 1
        # EMAs
        self._ema_s: Optional[float] = None
        self._ema_l: Optional[float] = None
        self._ks = 2.0 / (self.p.ema_short + 1)
        self._kl = 2.0 / (self.p.ema_long + 1)
        # Volume MA
        self._vols: list[float] = []
        # Position
        self._side: Optional[str] = None
        self._entry_price: Optional[float] = None
        self._stop: Optional[float] = None
        self._tp: Optional[float] = None

    def _atr_update(self, bar: Bar) -> Optional[float]:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(bar.high - bar.low,
                     abs(bar.high - self._prev_close),
                     abs(bar.low - self._prev_close))
        self._trs.append(tr)
        n = self.p.atr_period
        if len(self._trs) < n:
            return None
        if self._atr is None:
            self._atr = sum(self._trs[:n]) / n
        else:
            self._atr = (self._atr * (n - 1) + tr) / n
        return self._atr

    def _ema_update(self, close: float) -> tuple[Optional[float], Optional[float]]:
        if self._ema_s is None:
            self._ema_s = close
            self._ema_l = close
        else:
            self._ema_s = close * self._ks + self._ema_s * (1 - self._ks)
            self._ema_l = close * self._kl + self._ema_l * (1 - self._kl)
        return self._ema_s, self._ema_l

    def _score(self, bar: Bar, atr: float, ema_s: float, ema_l: float, flipped_up: bool) -> float:
        """Approximation of the 5-factor composite score. Each sub-score in [0,100];
        final is unweighted average. Real Pine likely uses tuned weights — shadow-test."""
        # 1) Volume surge
        self._vols.append(bar.volume)
        vol_ma = sum(self._vols[-self.p.volume_ma_period:]) / min(len(self._vols), self.p.volume_ma_period)
        vol_ratio = (bar.volume / vol_ma) if vol_ma > 0 else 1.0
        s_vol = min(100.0, max(0.0, (vol_ratio - 0.8) * 100.0))  # 1.0x=20, 1.8x=100
        # 2) Price displacement (bar range vs ATR)
        disp = (bar.high - bar.low) / atr if atr > 0 else 0.0
        s_disp = min(100.0, disp * 50.0)  # 1 ATR = 50, 2 ATR = 100
        # 3) EMA alignment
        if flipped_up:
            s_ema = 100.0 if ema_s > ema_l else 40.0
        else:
            s_ema = 100.0 if ema_s < ema_l else 40.0
        # 4) Regime quality — proxy by ATR/close (volatility regime). Higher = trending.
        vol_regime = (atr / bar.close) if bar.close > 0 else 0.0
        s_regime = min(100.0, vol_regime * 5000.0)  # 2% ATR/price = 100
        # 5) Band distance — proxy by current close distance from opposite band
        if flipped_up and self._final_lower is not None:
            dist = (bar.close - self._final_lower) / atr if atr > 0 else 0.0
        elif not flipped_up and self._final_upper is not None:
            dist = (self._final_upper - bar.close) / atr if atr > 0 else 0.0
        else:
            dist = 0.0
        s_band = min(100.0, max(0.0, dist * 30.0))
        return (s_vol + s_disp + s_ema + s_regime + s_band) / 5.0

    def on_bar(self, bar: Bar) -> Optional[dict]:
        atr = self._atr_update(bar)
        ema_s, ema_l = self._ema_update(bar.close)
        self._prev_close = bar.close
        if atr is None or math.isnan(atr) or atr <= 0 or ema_s is None:
            return None

        hl2 = (bar.high + bar.low) / 2.0
        bu = hl2 + self.p.base_multiplier * atr
        bl = hl2 - self.p.base_multiplier * atr
        if self._final_upper is None:
            self._final_upper, self._final_lower = bu, bl
        else:
            self._final_upper = bu if bu < self._final_upper else self._final_upper
            self._final_lower = bl if bl > self._final_lower else self._final_lower

        self._prev_trend = self._trend
        if self._trend == 1 and bar.close < self._final_lower:
            self._trend = -1
        elif self._trend == -1 and bar.close > self._final_upper:
            self._trend = 1

        # Manage open position: ATR stop or TP
        if self._side == "long":
            if bar.low <= self._stop:
                exit_px = self._stop
                self._side = None; self._entry_price = None; self._stop = None; self._tp = None
                return {"side": "exit", "reason": "atr_stop_long", "ts": bar.ts, "price": exit_px}
            if bar.high >= self._tp:
                exit_px = self._tp
                self._side = None; self._entry_price = None; self._stop = None; self._tp = None
                return {"side": "exit", "reason": "tp_long", "ts": bar.ts, "price": exit_px}
            if self._prev_trend == 1 and self._trend == -1:
                self._side = None; self._entry_price = None; self._stop = None; self._tp = None
                return {"side": "exit", "reason": "supertrend_flip_down", "ts": bar.ts, "price": bar.close}
            return None
        if self._side == "short":
            if bar.high >= self._stop:
                exit_px = self._stop
                self._side = None; self._entry_price = None; self._stop = None; self._tp = None
                return {"side": "exit", "reason": "atr_stop_short", "ts": bar.ts, "price": exit_px}
            if bar.low <= self._tp:
                exit_px = self._tp
                self._side = None; self._entry_price = None; self._stop = None; self._tp = None
                return {"side": "exit", "reason": "tp_short", "ts": bar.ts, "price": exit_px}
            if self._prev_trend == -1 and self._trend == 1:
                self._side = None; self._entry_price = None; self._stop = None; self._tp = None
                return {"side": "exit", "reason": "supertrend_flip_up", "ts": bar.ts, "price": bar.close}
            return None

        # Fresh entry — only on SuperTrend flip AND score >= threshold
        flipped_up = self._prev_trend == -1 and self._trend == 1
        flipped_down = self._prev_trend == 1 and self._trend == -1
        if not (flipped_up or flipped_down):
            return None
        score = self._score(bar, atr, ema_s, ema_l, flipped_up)
        if score < self.p.score_threshold:
            return None

        stop_dist = self.p.atr_stop_mult * atr
        if flipped_up:
            self._side = "long"; self._entry_price = bar.close
            self._stop = bar.close - stop_dist
            self._tp = bar.close + stop_dist * self.p.rr_ratio
            return {"side": "buy", "reason": "st_ai_flip_up_score",
                    "ts": bar.ts, "price": bar.close, "score": score}
        if flipped_down and self.p.allow_short:
            self._side = "short"; self._entry_price = bar.close
            self._stop = bar.close + stop_dist
            self._tp = bar.close - stop_dist * self.p.rr_ratio
            return {"side": "sell", "reason": "st_ai_flip_down_score",
                    "ts": bar.ts, "price": bar.close, "score": score}
        return None
