"""
RSI > 70 Buy / Exit on Cross Below 70 — BTCUSDT 4H
Author (Pine): Boubizee (TradingView, published 2025-12-29)
Source: https://www.tradingview.com/script/wZIdSrBG/
Minara backtest (1460d, HyperLiquid fees):
    APR +24.3% (Minara lead: +24.9%), Total +99.7%, Trades 142, WR 35.2%,
    Sharpe 1.85, MDD 14.8%. Fee drag 21%.

Inferred parameters (Pine source not visible, description-based):
    - RSI period 14 (standard)
    - Entry: RSI crosses above 70 (momentum continuation, NOT mean reversion)
    - Exit: RSI crosses back below 70
    - Long-only
    - No explicit stop loss

The insight: on BTC 4h, RSI>70 signals STRENGTH not exhaustion. Low WR (35%),
high PF — classic trend-following profile where a few big wins absorb losses.

Direction: LONG ONLY
Needs stop loss added? OPTIONAL — MDD already contained at 14.8% without one.
    If added, use wide (e.g., 2-3× ATR) so it doesn't truncate continuation legs.
    A tight SL would lower WR further without improving PF.
Fee sensitivity: MEDIUM. 142 trades / 4yr × 0.09% round-trip = ~3.2% fee drag/yr.
    Per-trade edge big enough to absorb it. Breakeven WR ≈ ~30% given PF profile.
Best venue: Hyperliquid (backtest venue). Bybit/Binance also viable since per-trade
    edge is large.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Rsi70ContBtc4HParams:
    rsi_period: int = 14
    rsi_threshold: float = 70.0
    long_only: bool = True


class Rsi70ContBtc4H:
    def __init__(self, params: Optional[Rsi70ContBtc4HParams] = None):
        self.p = params or Rsi70ContBtc4HParams()
        self._prev_close: Optional[float] = None
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._bar_count: int = 0
        self._seed_gains: list[float] = []
        self._seed_losses: list[float] = []
        self._prev_rsi: Optional[float] = None
        self._in_position: bool = False

    def _update_rsi(self, close: float) -> Optional[float]:
        if self._prev_close is None:
            self._prev_close = close
            return None
        change = close - self._prev_close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        self._prev_close = close
        self._bar_count += 1
        n = self.p.rsi_period

        if self._avg_gain is None:
            self._seed_gains.append(gain)
            self._seed_losses.append(loss)
            if len(self._seed_gains) < n:
                return None
            self._avg_gain = sum(self._seed_gains) / n
            self._avg_loss = sum(self._seed_losses) / n
        else:
            # Wilder's smoothing
            self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + loss) / n

        if self._avg_loss == 0:
            return 100.0 if self._avg_gain > 0 else 50.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def on_bar(self, bar: Bar) -> Optional[dict]:
        rsi = self._update_rsi(bar.close)
        if rsi is None:
            return None
        prev = self._prev_rsi
        self._prev_rsi = rsi
        if prev is None:
            return None

        crossed_above = prev <= self.p.rsi_threshold and rsi > self.p.rsi_threshold
        crossed_below = prev >= self.p.rsi_threshold and rsi < self.p.rsi_threshold

        if crossed_above and not self._in_position:
            self._in_position = True
            return {"side": "buy", "reason": "rsi_cross_above_70",
                    "ts": bar.ts, "price": bar.close, "rsi": rsi}
        if crossed_below and self._in_position:
            self._in_position = False
            return {"side": "exit", "reason": "rsi_cross_below_70",
                    "ts": bar.ts, "price": bar.close, "rsi": rsi}
        return None
