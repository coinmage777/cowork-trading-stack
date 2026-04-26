"""
Optimized BTC Mean Reversion (RSI 20/65) — BTCUSDT 15m
Author (Pine): malli_007 (TradingView, published 2025-12-13)
Source: https://www.tradingview.com/script/pIrgsDpT/
Minara backtest (90d, HyperLiquid fees):
    APR +204.6% (#1 on leaderboard), Trades 16 in 90d, Sharpe >4.
    Small sample — treat with skepticism.

Inferred parameters (TV description scrape):
    - RSI period 14
    - Long entry: RSI < 20 AND close > 200 EMA (only take mean-rev WITH the trend)
    - Short entry: RSI > 65 AND close < 200 EMA
    - Stochastic filter: K < 25 for longs, K > 75 for shorts (14,3,3 assumed)
    - Stop loss: 4%
    - Take profit: 6% (R:R 1:1.5)
    - No pyramiding

Direction: LONG and SHORT (regime-gated by 200 EMA)
Needs stop loss added? NO — already has 4% SL and 6% TP baked in.
Fee sensitivity: HIGH at scale. 16 trades in 90d extrapolates to ~65 trades/yr.
    Round-trip fee 0.09% × 65 ≈ 5.9% drag. Per-trade edge must exceed this
    comfortably. WR + R:R combo determines breakeven:
        breakeven WR = 4% / (4% + 6%) = 40% gross, ~42% after fees.
    Minara's +204% APR on 16 trades is a small sample; out-of-sample could
    easily halve it. Treat published Sharpe >4 with heavy discount.
Best venue: Hyperliquid (backtest). 15m cadence + 0.045% taker is the tightest
    fit. Bybit VIP0 (0.055%) already eats ~7% APR. Binance fine with VIP1+.
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
class MeanRevBtc15mParams:
    rsi_period: int = 14
    rsi_long_entry: float = 20.0
    rsi_short_entry: float = 65.0
    ema_period: int = 200
    stoch_k_period: int = 14
    stoch_d_smooth: int = 3
    stoch_long_max: float = 25.0
    stoch_short_min: float = 75.0
    stop_loss_pct: float = 0.04
    take_profit_pct: float = 0.06


class MeanRevBtc15m:
    def __init__(self, params: Optional[MeanRevBtc15mParams] = None):
        self.p = params or MeanRevBtc15mParams()
        # RSI state
        self._prev_close: Optional[float] = None
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._seed_g: list[float] = []
        self._seed_l: list[float] = []
        # EMA state
        self._ema: Optional[float] = None
        self._ema_k = 2.0 / (self.p.ema_period + 1)
        self._ema_seed: list[float] = []
        # Stoch state (rolling highs/lows)
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._k_values: list[float] = []
        # Position
        self._side: Optional[str] = None  # 'long' | 'short' | None
        self._entry: Optional[float] = None

    def _update_rsi(self, close: float) -> Optional[float]:
        if self._prev_close is None:
            self._prev_close = close
            return None
        ch = close - self._prev_close
        g, l = max(ch, 0.0), max(-ch, 0.0)
        self._prev_close = close
        n = self.p.rsi_period
        if self._avg_gain is None:
            self._seed_g.append(g); self._seed_l.append(l)
            if len(self._seed_g) < n:
                return None
            self._avg_gain = sum(self._seed_g) / n
            self._avg_loss = sum(self._seed_l) / n
        else:
            self._avg_gain = (self._avg_gain * (n - 1) + g) / n
            self._avg_loss = (self._avg_loss * (n - 1) + l) / n
        if self._avg_loss == 0:
            return 100.0 if self._avg_gain > 0 else 50.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def _update_ema(self, close: float) -> Optional[float]:
        n = self.p.ema_period
        if self._ema is None:
            self._ema_seed.append(close)
            if len(self._ema_seed) < n:
                return None
            self._ema = sum(self._ema_seed) / n
            return self._ema
        self._ema = close * self._ema_k + self._ema * (1 - self._ema_k)
        return self._ema

    def _update_stoch_k(self, bar: Bar) -> Optional[float]:
        self._highs.append(bar.high); self._lows.append(bar.low)
        n = self.p.stoch_k_period
        if len(self._highs) < n:
            return None
        hh = max(self._highs[-n:]); ll = min(self._lows[-n:])
        if hh == ll:
            raw_k = 50.0
        else:
            raw_k = 100.0 * (bar.close - ll) / (hh - ll)
        self._k_values.append(raw_k)
        d = self.p.stoch_d_smooth
        if len(self._k_values) < d:
            return raw_k
        return sum(self._k_values[-d:]) / d  # %K smoothed

    def on_bar(self, bar: Bar) -> Optional[dict]:
        rsi = self._update_rsi(bar.close)
        ema = self._update_ema(bar.close)
        k = self._update_stoch_k(bar)
        if rsi is None or ema is None or k is None:
            return None

        # Manage open position (SL/TP)
        if self._side == "long":
            if bar.low <= self._entry * (1 - self.p.stop_loss_pct):
                self._side = None
                px = self._entry * (1 - self.p.stop_loss_pct)
                self._entry = None
                return {"side": "exit", "reason": "sl_long", "ts": bar.ts, "price": px}
            if bar.high >= self._entry * (1 + self.p.take_profit_pct):
                self._side = None
                px = self._entry * (1 + self.p.take_profit_pct)
                self._entry = None
                return {"side": "exit", "reason": "tp_long", "ts": bar.ts, "price": px}
            return None
        if self._side == "short":
            if bar.high >= self._entry * (1 + self.p.stop_loss_pct):
                self._side = None
                px = self._entry * (1 + self.p.stop_loss_pct)
                self._entry = None
                return {"side": "exit", "reason": "sl_short", "ts": bar.ts, "price": px}
            if bar.low <= self._entry * (1 - self.p.take_profit_pct):
                self._side = None
                px = self._entry * (1 - self.p.take_profit_pct)
                self._entry = None
                return {"side": "exit", "reason": "tp_short", "ts": bar.ts, "price": px}
            return None

        # Entries (flat)
        long_trigger = rsi < self.p.rsi_long_entry and bar.close > ema and k < self.p.stoch_long_max
        short_trigger = rsi > self.p.rsi_short_entry and bar.close < ema and k > self.p.stoch_short_min
        if long_trigger:
            self._side = "long"; self._entry = bar.close
            return {"side": "buy", "reason": "rsi20_ema_stoch_long", "ts": bar.ts, "price": bar.close}
        if short_trigger:
            self._side = "short"; self._entry = bar.close
            return {"side": "sell", "reason": "rsi65_ema_stoch_short", "ts": bar.ts, "price": bar.close}
        return None
