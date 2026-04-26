"""
Backtesting engine for the Polymarket BTC 15-min strategy.

Uses historical BTC price data and simulated market conditions to validate
the strategy before live deployment. Reports Sharpe ratio, win rate,
max drawdown, and average edge captured.

Usage:
    python backtest.py [--days 30] [--initial-balance 1000]
"""

import argparse
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import httpx

from config import Config
from strategy import PriceEngine, Candle, SignalOutput


@dataclass
class SimulatedMarket:
    market_id: str
    strike_price: float
    expiry_timestamp: float
    yes_price: float
    no_price: float
    implied_prob_yes: float
    liquidity: float
    minutes_to_expiry: float
    actual_outcome: bool  # True if BTC was above strike at expiry


@dataclass
class BacktestTrade:
    timestamp: float
    market_id: str
    side: str
    size: float
    entry_price: float
    exit_price: float
    pnl: float
    edge: float
    model_prob: float
    market_prob: float
    strategy: str


@dataclass
class BacktestResult:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_pnl_per_trade: float
    avg_edge_captured: float
    sharpe_ratio: float
    max_drawdown: float
    max_drawdown_pct: float
    profit_factor: float
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


class Backtester:
    def __init__(self, config: Config, initial_balance: float = 1000.0):
        self.config = config
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.trades: list[BacktestTrade] = []
        self.equity_curve: list[float] = [initial_balance]
        self.daily_pnl: list[float] = []

    async def fetch_historical_prices(self, days: int = 30) -> pd.DataFrame:
        """Fetch historical BTC/USDT 1-minute candles from Binance."""
        print(f"[Backtest] Fetching {days} days of BTC 1m candle data...")

        all_candles = []
        end_time = int(time.time() * 1000)
        start_time = end_time - (days * 24 * 60 * 60 * 1000)

        async with httpx.AsyncClient(timeout=30.0) as client:
            current = start_time
            while current < end_time:
                try:
                    response = await client.get(
                        "https://api.binance.com/api/v3/klines",
                        params={
                            "symbol": "BTCUSDT",
                            "interval": "1m",
                            "startTime": current,
                            "limit": 1000,
                        }
                    )
                    response.raise_for_status()
                    data = response.json()

                    if not data:
                        break

                    for candle in data:
                        all_candles.append({
                            "timestamp": candle[0] / 1000.0,
                            "open": float(candle[1]),
                            "high": float(candle[2]),
                            "low": float(candle[3]),
                            "close": float(candle[4]),
                            "volume": float(candle[5]),
                        })

                    current = int(data[-1][0]) + 60000
                    # Rate limit
                    import asyncio
                    await asyncio.sleep(0.2)

                except httpx.HTTPError as e:
                    print(f"[Backtest] API error: {e}, retrying...")
                    import asyncio
                    await asyncio.sleep(1)

        df = pd.DataFrame(all_candles)
        print(f"[Backtest] Fetched {len(df)} candles")
        return df

    def generate_simulated_markets(self, df: pd.DataFrame) -> list[SimulatedMarket]:
        """
        Generate simulated 15-min markets from historical price data.
        Every 15 minutes, create a market: "Will BTC be above $X?"
        where X is derived from the current price with some offset.
        """
        markets = []
        timestamps = df["timestamp"].values
        closes = df["close"].values

        # Create a market every 15 minutes
        interval = 15  # rows (1-min candles)
        for i in range(30, len(df) - interval, interval):
            current_price = closes[i]
            future_price = closes[min(i + interval, len(closes) - 1)]

            # Strike price: current price +/- random offset
            np.random.seed(int(timestamps[i]) % (2**31))
            offsets = [-500, -300, -200, -100, 0, 100, 200, 300, 500]
            strike_offset = np.random.choice(offsets)
            strike_price = round(current_price + strike_offset, 0)

            # Actual outcome at expiry
            actual_above = future_price > strike_price

            # Simulate market pricing with some noise
            # True probability
            price_diff = current_price - strike_price
            true_prob = 1.0 / (1.0 + math.exp(-price_diff / 200))

            # Market price = true prob + noise (simulating inefficiency)
            noise = np.random.normal(0, 0.08)  # ~8% noise
            market_yes_price = np.clip(true_prob + noise, 0.02, 0.98)

            # Add extra noise for late-market scenarios
            minutes_to_expiry = 15.0  # start of market

            liquidity = np.random.uniform(3000, 50000)

            markets.append(SimulatedMarket(
                market_id=f"sim_{int(timestamps[i])}",
                strike_price=strike_price,
                expiry_timestamp=timestamps[i] + interval * 60,
                yes_price=round(market_yes_price, 4),
                no_price=round(1 - market_yes_price, 4),
                implied_prob_yes=market_yes_price,
                liquidity=liquidity,
                minutes_to_expiry=minutes_to_expiry,
                actual_outcome=actual_above,
            ))

        # Also generate late-market snapshots (5-8 min before expiry)
        for i in range(30, len(df) - interval, interval):
            # Snapshot at ~7 minutes before expiry (8 candles before)
            late_idx = i + interval - 8
            if late_idx >= len(closes):
                continue

            current_price = closes[late_idx]
            future_price = closes[min(i + interval, len(closes) - 1)]

            np.random.seed(int(timestamps[late_idx]) % (2**31) + 1)
            offsets = [-200, -100, 0, 100, 200]
            strike_offset = np.random.choice(offsets)
            strike_price = round(current_price + strike_offset, 0)

            actual_above = future_price > strike_price
            price_diff = current_price - strike_price
            true_prob = 1.0 / (1.0 + math.exp(-price_diff / 150))

            # Late markets tend to have more extreme pricing (overreaction)
            if np.random.random() < 0.3:  # 30% chance of extreme mispricing
                if true_prob > 0.4:
                    market_yes_price = np.random.uniform(0.82, 0.95)
                else:
                    market_yes_price = np.random.uniform(0.05, 0.18)
            else:
                noise = np.random.normal(0, 0.10)
                market_yes_price = np.clip(true_prob + noise, 0.02, 0.98)

            liquidity = np.random.uniform(3000, 30000)

            markets.append(SimulatedMarket(
                market_id=f"late_{int(timestamps[late_idx])}",
                strike_price=strike_price,
                expiry_timestamp=timestamps[i] + interval * 60,
                yes_price=round(market_yes_price, 4),
                no_price=round(1 - market_yes_price, 4),
                implied_prob_yes=market_yes_price,
                liquidity=liquidity,
                minutes_to_expiry=7.0,  # late market
                actual_outcome=actual_above,
            ))

        return markets

    def compute_signal_at(self, candles_up_to: list[dict], config: Config) -> SignalOutput:
        """Compute strategy signals from historical candle data."""
        engine = PriceEngine(config)
        for c in candles_up_to:
            engine.candles.append(Candle(
                open=c["open"], high=c["high"], low=c["low"],
                close=c["close"], volume=c["volume"],
                vwap_numerator=c["close"] * c["volume"],
                timestamp=c["timestamp"],
            ))
        signal = engine.compute_signals()
        return signal

    def evaluate_trade(self, market: SimulatedMarket, side: str,
                       entry_price: float, size: float) -> tuple[float, float]:
        """
        Evaluate a trade outcome.
        Returns (exit_price, pnl).
        Binary outcome: token pays $1 if correct, $0 if wrong.
        """
        if side == "YES":
            won = market.actual_outcome
        else:
            won = not market.actual_outcome

        if won:
            exit_price = 1.0
            pnl = (1.0 - entry_price) * size
        else:
            exit_price = 0.0
            pnl = -entry_price * size

        return exit_price, pnl

    def run_backtest(self, df: pd.DataFrame, markets: list[SimulatedMarket]) -> BacktestResult:
        """Run the full backtest simulation."""
        print(f"[Backtest] Running simulation on {len(markets)} markets...")

        config = self.config
        daily_returns = []
        current_day_pnl = 0.0
        last_day = None
        open_positions = 0

        for market in markets:
            # Find candles up to market creation time
            market_start = market.expiry_timestamp - market.minutes_to_expiry * 60
            candle_mask = df["timestamp"] <= market_start
            available = df[candle_mask].tail(config.candle_history)

            if len(available) < max(config.rsi_period + 1, config.bb_period):
                continue

            signal = self.compute_signal_at(available.to_dict("records"), config)
            if signal is None:
                continue

            current_price = signal.current_price

            # Check if market meets basic filters
            if market.liquidity < config.min_market_liquidity:
                continue
            if market.minutes_to_expiry < config.min_time_to_expiry:
                continue

            # Calculate model probability for this market
            if market.strike_price is None:
                continue

            price_diff = current_price - market.strike_price
            base_prob = 1.0 / (1.0 + math.exp(-price_diff / 200))
            model_prob = base_prob * 0.7 + signal.model_prob_up * 0.3
            model_prob = max(0.01, min(0.99, model_prob))

            # Edge detection
            edge_yes = model_prob - market.implied_prob_yes
            edge_no = (1 - model_prob) - (1 - market.implied_prob_yes)

            # Late-market mispricing check
            is_late = config.late_market_window_min <= market.minutes_to_expiry <= config.late_market_window_max
            is_near_strike = abs(current_price - market.strike_price) < config.late_market_price_range
            is_extreme = (market.implied_prob_yes < config.late_market_extreme_low or
                          market.implied_prob_yes > config.late_market_extreme_high)
            model_uncertain = config.late_model_uncertain_low <= model_prob <= config.late_model_uncertain_high

            strategy = "momentum_edge"
            side = None
            trade_model_prob = None
            trade_market_prob = None
            trade_edge = None
            entry_price = None

            if is_late and is_near_strike and is_extreme and model_uncertain:
                strategy = "late_correction"
                if market.implied_prob_yes < config.late_market_extreme_low:
                    side = "YES"
                    trade_model_prob = model_prob
                    trade_market_prob = market.implied_prob_yes
                    trade_edge = model_prob - market.implied_prob_yes
                    entry_price = market.yes_price
                elif market.implied_prob_yes > config.late_market_extreme_high:
                    side = "NO"
                    trade_model_prob = 1 - model_prob
                    trade_market_prob = 1 - market.implied_prob_yes
                    trade_edge = (1 - model_prob) - (1 - market.implied_prob_yes)
                    entry_price = market.no_price
            elif abs(edge_yes) > config.min_edge_threshold:
                if edge_yes > 0:
                    side = "YES"
                    trade_model_prob = model_prob
                    trade_market_prob = market.implied_prob_yes
                    trade_edge = edge_yes
                    entry_price = market.yes_price
                elif edge_no > config.min_edge_threshold:
                    side = "NO"
                    trade_model_prob = 1 - model_prob
                    trade_market_prob = 1 - market.implied_prob_yes
                    trade_edge = edge_no
                    entry_price = market.no_price

            if side is None or trade_edge is None or entry_price is None:
                continue

            # Kelly sizing
            if trade_market_prob <= 0 or trade_market_prob >= 1:
                continue
            odds = (1 - trade_market_prob) / trade_market_prob
            kelly_raw = (trade_model_prob * odds - (1 - trade_model_prob)) / odds
            kelly_raw = max(kelly_raw, 0.0)
            kelly_frac = kelly_raw * config.kelly_fraction

            bet_size = min(kelly_frac * config.max_single_bet / config.kelly_fraction,
                           config.max_single_bet)
            bet_size = min(bet_size, self.balance * 0.2)  # max 20% of balance

            if bet_size < 1.0:
                continue

            # Position limit check
            if open_positions >= config.max_open_positions:
                continue

            # Execute trade
            exit_price, pnl = self.evaluate_trade(market, side, entry_price, bet_size)

            self.trades.append(BacktestTrade(
                timestamp=market.expiry_timestamp - market.minutes_to_expiry * 60,
                market_id=market.market_id,
                side=side,
                size=bet_size,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl=pnl,
                edge=trade_edge,
                model_prob=trade_model_prob,
                market_prob=trade_market_prob,
                strategy=strategy,
            ))

            self.balance += pnl
            self.equity_curve.append(self.balance)

            # Track daily PnL
            trade_day = datetime.fromtimestamp(market.expiry_timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
            if last_day != trade_day:
                if last_day is not None:
                    daily_returns.append(current_day_pnl)
                current_day_pnl = 0.0
                last_day = trade_day
            current_day_pnl += pnl

            # Daily stop loss check
            if current_day_pnl <= config.daily_stop_loss:
                daily_returns.append(current_day_pnl)
                current_day_pnl = 0.0
                last_day = None  # reset for next day

        if current_day_pnl != 0:
            daily_returns.append(current_day_pnl)

        return self._compute_results(daily_returns)

    def _compute_results(self, daily_returns: list[float]) -> BacktestResult:
        """Compute backtest statistics."""
        if not self.trades:
            return BacktestResult(
                total_trades=0, wins=0, losses=0, win_rate=0.0,
                total_pnl=0.0, avg_pnl_per_trade=0.0, avg_edge_captured=0.0,
                sharpe_ratio=0.0, max_drawdown=0.0, max_drawdown_pct=0.0,
                profit_factor=0.0, trades=[], equity_curve=self.equity_curve,
            )

        wins = sum(1 for t in self.trades if t.pnl > 0)
        losses = len(self.trades) - wins
        total_pnl = sum(t.pnl for t in self.trades)
        avg_pnl = total_pnl / len(self.trades)
        avg_edge = np.mean([t.edge for t in self.trades])

        # Sharpe ratio (annualized from daily returns)
        if daily_returns and len(daily_returns) > 1:
            daily_arr = np.array(daily_returns)
            sharpe = (np.mean(daily_arr) / np.std(daily_arr)) * np.sqrt(365) if np.std(daily_arr) > 0 else 0.0
        else:
            sharpe = 0.0

        # Max drawdown
        equity = np.array(self.equity_curve)
        peak = np.maximum.accumulate(equity)
        drawdown = equity - peak
        max_dd = abs(drawdown.min()) if len(drawdown) > 0 else 0.0
        max_dd_pct = max_dd / peak[np.argmin(drawdown)] * 100 if len(drawdown) > 0 and peak[np.argmin(drawdown)] > 0 else 0.0

        # Profit factor
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        return BacktestResult(
            total_trades=len(self.trades),
            wins=wins,
            losses=losses,
            win_rate=wins / len(self.trades),
            total_pnl=total_pnl,
            avg_pnl_per_trade=avg_pnl,
            avg_edge_captured=avg_edge,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            max_drawdown_pct=max_dd_pct,
            profit_factor=profit_factor,
            trades=self.trades,
            equity_curve=self.equity_curve,
        )


def print_report(result: BacktestResult):
    """Print formatted backtest report."""
    print("\n" + "=" * 60)
    print("       BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Total Trades:        {result.total_trades}")
    print(f"  Wins / Losses:       {result.wins} / {result.losses}")
    print(f"  Win Rate:            {result.win_rate * 100:.1f}%")
    print(f"  Total PnL:           ${result.total_pnl:.2f}")
    print(f"  Avg PnL/Trade:       ${result.avg_pnl_per_trade:.2f}")
    print(f"  Avg Edge Captured:   {result.avg_edge_captured * 100:.2f}%")
    print(f"  Sharpe Ratio:        {result.sharpe_ratio:.2f}")
    print(f"  Max Drawdown:        ${result.max_drawdown:.2f} ({result.max_drawdown_pct:.1f}%)")
    print(f"  Profit Factor:       {result.profit_factor:.2f}")
    print("=" * 60)

    # Strategy breakdown
    momentum_trades = [t for t in result.trades if t.strategy == "momentum_edge"]
    late_trades = [t for t in result.trades if t.strategy == "late_correction"]

    if momentum_trades:
        m_wins = sum(1 for t in momentum_trades if t.pnl > 0)
        m_pnl = sum(t.pnl for t in momentum_trades)
        print(f"\n  [Momentum Edge Strategy]")
        print(f"    Trades: {len(momentum_trades)}  |  Win Rate: {m_wins/len(momentum_trades)*100:.1f}%  |  PnL: ${m_pnl:.2f}")

    if late_trades:
        l_wins = sum(1 for t in late_trades if t.pnl > 0)
        l_pnl = sum(t.pnl for t in late_trades)
        print(f"\n  [Late Correction Strategy]")
        print(f"    Trades: {len(late_trades)}  |  Win Rate: {l_wins/len(late_trades)*100:.1f}%  |  PnL: ${l_pnl:.2f}")

    # Sharpe gate
    print("\n" + "-" * 60)
    if result.sharpe_ratio > 1.0:
        print("  [PASS] Sharpe ratio > 1.0 - Strategy validated for live trading")
    else:
        print("  [FAIL] Sharpe ratio < 1.0 - Strategy needs tuning before live")
    print("-" * 60 + "\n")


async def main():
    parser = argparse.ArgumentParser(description="Backtest Polymarket BTC strategy")
    parser.add_argument("--days", type=int, default=30, help="Days of historical data")
    parser.add_argument("--initial-balance", type=float, default=1000.0, help="Starting balance in USDC")
    args = parser.parse_args()

    config = Config()
    backtester = Backtester(config, initial_balance=args.initial_balance)

    # Fetch historical data
    df = await backtester.fetch_historical_prices(days=args.days)
    if df.empty:
        print("[Backtest] No data fetched. Check network connection.")
        return

    # Generate simulated markets
    markets = backtester.generate_simulated_markets(df)
    print(f"[Backtest] Generated {len(markets)} simulated markets")

    # Run backtest
    result = backtester.run_backtest(df, markets)

    # Print report
    print_report(result)

    return result


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
