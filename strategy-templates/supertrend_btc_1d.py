"""
SuperTrend STRATEGY — BTCUSDT 1D
Author (Pine): holdon_to_profits (TradingView, published 2026-02-11)
Source: https://www.tradingview.com/script/VLRj2sG9/
Minara backtest (1460d, HyperLiquid fees 0.015m/0.045t):
    APR +35.6%, Total +292.4%, Trades 4, PF 8.98, Sharpe 1.24, MDD 46.1%, WR 75% (3/4)

Inferred parameters (Minara reports ATR=10, mult=8.5 — "ignores almost everything,
only major trend reversals trigger"):
    - SuperTrend source = hl2
    - ATR period = 10
    - Multiplier = 8.5 (extreme — lower multipliers would trade more often)
    - Long-only
    - No explicit stop loss / take profit — exit is pure SuperTrend flip

Direction: LONG ONLY
Needs stop loss added? NO — high multiplier already acts as an implicit trailing stop
    via SuperTrend flip. MDD 46% is structural; adding a tight SL would kill the edge.
Fee sensitivity: EXTREMELY LOW. 8 fills over 4 years. Breakeven WR after 0.045%
    taker ≈ trivial; any win absorbs decades of fees. Safest first-LIVE candidate.
Best venue: Hyperliquid (0.045% taker matches backtest). Binance/Bybit also fine —
    this is one of the few strategies where fee tier is irrelevant.
"""
from dataclasses import dataclass, field
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
class SuperTrendBtc1DParams:
    atr_period: int = 10
    multiplier: float = 8.5
    long_only: bool = True


class SuperTrendBtc1D:
    """Pine v5 SuperTrend reimplemented. Uses Wilder's RMA for ATR (matches
    TV's ta.atr). Band math follows ta.supertrend: basic upper/lower with
    carry-forward locking."""

    def __init__(self, params: Optional[SuperTrendBtc1DParams] = None):
        self.p = params or SuperTrendBtc1DParams()
        self._trs: list[float] = []
        self._atr: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._final_upper: Optional[float] = None
        self._final_lower: Optional[float] = None
        self._trend: int = 1  # 1 = up, -1 = down
        self._prev_trend: int = 1
        self._in_position: bool = False

    def _update_atr(self, bar: Bar) -> Optional[float]:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._trs.append(tr)
        n = self.p.atr_period
        if len(self._trs) < n:
            return None
        if self._atr is None:
            self._atr = sum(self._trs[:n]) / n
        else:
            # Wilder's RMA: atr = (prev_atr * (n-1) + tr) / n
            self._atr = (self._atr * (n - 1) + tr) / n
        return self._atr

    def on_bar(self, bar: Bar) -> Optional[dict]:
        # Capture PREVIOUS close before we update ATR (ATR uses prev close for TR)
        prev_close = self._prev_close
        atr = self._update_atr(bar)
        if atr is None or math.isnan(atr) or atr <= 0:
            self._prev_close = bar.close
            return None

        hl2 = (bar.high + bar.low) / 2.0
        basic_upper = hl2 + self.p.multiplier * atr
        basic_lower = hl2 - self.p.multiplier * atr

        # Final bands: carry-forward locking (Pine ta.supertrend)
        if self._final_upper is None:
            self._final_upper = basic_upper
            self._final_lower = basic_lower
        else:
            self._final_upper = (
                basic_upper
                if basic_upper < self._final_upper or (prev_close is not None and prev_close > self._final_upper)
                else self._final_upper
            )
            self._final_lower = (
                basic_lower
                if basic_lower > self._final_lower or (prev_close is not None and prev_close < self._final_lower)
                else self._final_lower
            )

        self._prev_trend = self._trend
        if self._trend == 1 and bar.close < self._final_lower:
            self._trend = -1
        elif self._trend == -1 and bar.close > self._final_upper:
            self._trend = 1

        # Update prev_close AFTER band computation for next bar
        self._prev_close = bar.close

        # Signals on trend flip (entry on confirmed close)
        flipped_up = self._prev_trend == -1 and self._trend == 1
        flipped_down = self._prev_trend == 1 and self._trend == -1

        if flipped_up and not self._in_position:
            self._in_position = True
            return {"side": "buy", "reason": "supertrend_flip_up", "ts": bar.ts, "price": bar.close}
        if flipped_down and self._in_position:
            self._in_position = False
            return {"side": "exit", "reason": "supertrend_flip_down", "ts": bar.ts, "price": bar.close}
        return None
