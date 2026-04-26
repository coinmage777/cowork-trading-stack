"""
BB Upper breakout Short +2% — SOLUSDT 1H
Author (Pine): dr_Ziuber (TradingView, published 2026-01-22, https://x.com/drziuber)
Source: https://www.tradingview.com/script/UBGvlIlq/
Minara backtest (730d, HyperLiquid fees):
    APR +48.1%, Total +96.3%, Trades 49, WR 100% (49/49), MDD 36.7%
    Fee drag only 3%.

Inferred parameters (Pine source not visible, description-based):
    - Bollinger Bands: period 20, stddev 2 (TV defaults)
    - Entry: close > upper_band * 1.02  (price exceeds upper BB by >2%)
    - Exit: +2% profit from entry
    - Short-only (textbook mean reversion on overextensions)
    - NO stop loss in Pine — this is the structural risk.

Direction: SHORT ONLY
Needs stop loss added? YES — CRITICAL. 100% WR over 49 trades does NOT imply
    next 49 will all win. MDD 36.7% means one trade sat deep underwater before
    reverting. For live, add a hard stop (e.g., 8-12% or 2× ATR) to cap tail
    risk. Minara explicitly flagged this as a caveat.
Fee sensitivity: LOW. ~25 trades/yr × 0.09% round-trip = ~2.2% fee drag/yr.
    Breakeven WR with 2% TP and 0.1% taker round-trip ≈ 2.5% / (2% + 2.5%) → 55%.
    At 100% WR historically there's huge buffer, but the moment it breaks the
    buffer disappears fast.
Best venue: Hyperliquid (matches backtest exactly). Avoid Bybit/Binance VIP0
    (taker 0.055%+) — would compress the already-thin per-trade edge when a
    stop loss is added and WR drops to realistic 70-85%.
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
class BBUpperShortSol1HParams:
    bb_period: int = 20
    bb_stddev: float = 2.0
    breakout_pct: float = 0.02       # 2% above upper band
    take_profit_pct: float = 0.02    # +2% of entry for short = price drops 2%
    hard_stop_pct: float = 0.10      # ADDED — not in original Pine, recommended for live
    use_hard_stop: bool = True


class BBUpperShortSol1H:
    def __init__(self, params: Optional[BBUpperShortSol1HParams] = None):
        self.p = params or BBUpperShortSol1HParams()
        self._closes: list[float] = []
        self._entry_price: Optional[float] = None  # None = flat

    def _bb_upper(self) -> Optional[float]:
        n = self.p.bb_period
        if len(self._closes) < n:
            return None
        window = self._closes[-n:]
        mean = sum(window) / n
        var = sum((x - mean) ** 2 for x in window) / n
        sd = math.sqrt(var)
        return mean + self.p.bb_stddev * sd

    def on_bar(self, bar: Bar) -> Optional[dict]:
        self._closes.append(bar.close)
        upper = self._bb_upper()
        if upper is None:
            return None

        # Manage open short
        if self._entry_price is not None:
            # Take profit: price fell 2% from entry
            if bar.low <= self._entry_price * (1 - self.p.take_profit_pct):
                tp_price = self._entry_price * (1 - self.p.take_profit_pct)
                self._entry_price = None
                return {"side": "exit", "reason": "tp_2pct", "ts": bar.ts, "price": tp_price}
            # Hard stop (added — not in Pine): price rose X% above entry
            if self.p.use_hard_stop and bar.high >= self._entry_price * (1 + self.p.hard_stop_pct):
                sl_price = self._entry_price * (1 + self.p.hard_stop_pct)
                self._entry_price = None
                return {"side": "exit", "reason": "hard_stop", "ts": bar.ts, "price": sl_price}
            return None

        # Entry: close > upper * (1 + breakout_pct), short it
        trigger = upper * (1 + self.p.breakout_pct)
        if bar.close > trigger:
            self._entry_price = bar.close
            return {"side": "sell", "reason": "bb_upper_breakout_2pct",
                    "ts": bar.ts, "price": bar.close,
                    "bb_upper": upper, "trigger": trigger}
        return None
