# 08. Minara Backtesting

Going live without a backtest makes me a gambler, not a trader. The backtesting workflow I use, and what I've learned along the way.

## Why backtesting matters

I once skipped backtest, went live, and produced 27 consecutive losing days. Cause: a TP/SL ratio that required 94% break-even win rate, only realized through live data. One hour of backtesting would have caught it on the spot.

Backtesting answers:
- What's this strategy's expected win rate?
- Average win vs average loss?
- Break-even win rate?
- Max Drawdown?
- How does it perform in different volatility / trend regimes?

## Backtest pitfalls

### 1) Data quality
- **Common mistake**: 1-minute OHLCV-based backtest → real entry could be anywhere between high and low
- **Better**: tick or 1-second data; if not, simulate both high and low cases

### 2) Ignoring slippage / fees
- **Common mistake**: assume market orders fill at mid
- **Better**: subtract fees every fill, apply slippage scaled with size

### 3) Look-ahead bias
- **Common mistake**: signal computed on the close → you didn't have the close yet at decision time
- **Better**: signals always use bars before the current decision point

### 4) Overfitting
- **Common mistake**: grid search on 1 year of data → "optimal params" only optimal for that year
- **Better**:
  - Train / Validation / Test split (e.g., 70/15/15)
  - Walk-forward analysis (train → test → next train → next test, in time order)
  - Robustness check (small param changes shouldn't blow up PnL)

### 5) Survivorship bias
- **Common mistake**: backtest only currently-listed assets
- **Better**: include delisted tokens / vanished exchanges in your data set

## My backtest workflow

### Step 1: data collection

OHLCV from exchange APIs:

```python
import ccxt
import pandas as pd

ex = ccxt.binance()
since = ex.parse8601("2024-01-01T00:00:00Z")
all_candles = []
while True:
    candles = ex.fetch_ohlcv("BTC/USDT", timeframe="1m", since=since, limit=1000)
    if not candles:
        break
    all_candles += candles
    since = candles[-1][0] + 60_000
df = pd.DataFrame(all_candles, columns=["ts", "o", "h", "l", "c", "v"])
df.to_parquet("btc_1m.parquet")
```

Mind exchange rate limits — sleep between calls.

### Step 2: signal backtest

Basic frame:

```python
def backtest(df, signal_fn, entry_threshold, exit_threshold, fee=0.0005, slip=0.0002):
    position = 0  # +1 long, -1 short, 0 flat
    entry_price = 0
    pnl_log = []
    
    for i in range(len(df)):
        signal = signal_fn(df.iloc[:i+1])  # only data up to i
        price = df.iloc[i]["c"]
        
        if position == 0:
            if signal > entry_threshold:
                position = 1
                entry_price = price * (1 + slip)
            elif signal < -entry_threshold:
                position = -1
                entry_price = price * (1 - slip)
        else:
            pnl_pct = (price - entry_price) / entry_price * position
            if abs(signal) < exit_threshold or pnl_pct < -0.025:  # SL
                exit_price = price * (1 - slip * position)
                trade_pnl = (exit_price - entry_price) / entry_price * position - 2 * fee
                pnl_log.append(trade_pnl)
                position = 0
    
    return pnl_log
```

### Step 3: metrics

```python
def metrics(pnl_log):
    pnl_arr = np.array(pnl_log)
    wins = pnl_arr[pnl_arr > 0]
    losses = pnl_arr[pnl_arr < 0]
    
    return {
        "n_trades": len(pnl_arr),
        "win_rate": len(wins) / len(pnl_arr) if len(pnl_arr) else 0,
        "avg_win": wins.mean() if len(wins) else 0,
        "avg_loss": losses.mean() if len(losses) else 0,
        "profit_factor": -wins.sum() / losses.sum() if losses.sum() < 0 else float("inf"),
        "total_return": pnl_arr.sum(),
        "max_drawdown": (np.maximum.accumulate(pnl_arr.cumsum()) - pnl_arr.cumsum()).max(),
        "sharpe_approx": pnl_arr.mean() / pnl_arr.std() * np.sqrt(365 * 24 * 60) if pnl_arr.std() > 0 else 0,
    }
```

Targets:
- **Profit Factor > 1.5**: meaningful alpha (after fees + slippage)
- **Max Drawdown < 30%**: tolerable from a capital perspective
- **Sharpe > 1.5**: solid return-to-volatility (crypto can support higher)

### Step 4: walk-forward

Split full period into N chunks → each train / test → measure out-of-sample.

```python
def walk_forward(df, signal_fn, n_splits=5):
    chunk = len(df) // n_splits
    oos_returns = []
    for i in range(n_splits - 1):
        train = df.iloc[i*chunk:(i+1)*chunk]
        test = df.iloc[(i+1)*chunk:(i+2)*chunk]
        best_params = optimize(train, signal_fn)
        oos_returns.append(backtest(test, signal_fn, **best_params))
    return oos_returns
```

If OOS PnL is materially worse than IS → overfit. Redesign.

### Step 5: simulation → paper → live

Backtest passes → paper trade (live prices, fake balance) for 1–2 weeks → small live → verify → scale.

If paper diverges from backtest, your data / signal / slippage assumptions are wrong somewhere.

## Minara — my backtesting tool

I built a backtesting module for the multi-exchange pair trading bot, called Minara internally. Features:

- **Multi-exchange synchronized data** — same-timestamp prices across N exchanges
- **Pair signals**: momentum_diff / spread_zscore / volatility_ratio etc.
- **Regime filter sim** — measures correlation-based entry blocking effect
- **DCA sim** — effect of additional entries / max_entries variations
- **Trailing stop sim** — activation / callback variations
- **Fee / funding modeling** — per-exchange accurate fees + funding time series
- **Stagger / size multiplier sim** — group dispersion effect

### Input / output

Input:
```yaml
data:
  exchanges: [hyperliquid, binance]
  symbols: [BTC, ETH]
  timeframe: 1m
  start: 2024-01-01
  end: 2024-12-31

strategy:
  signal: spread_zscore
  entry_threshold: 1.5
  exit_threshold: 0.3
  stop_loss: -0.025
  trailing:
    activation: 0.015
    callback: 0.010
  dca:
    max_entries: 3
    additional_threshold: 0.003

filters:
  regime:
    enabled: true
    correlation_window: 24
    min_correlation: 0.7

execution:
  fee_maker: 0.0001
  fee_taker: 0.0005
  slippage_pct: 0.0002
```

Output:
- Per-trade log (CSV / parquet)
- Metric summary (JSON)
- Equity curve chart
- Distribution stats (PnL histogram, holding-time distribution, etc.)
- Parameter sensitivity charts

### Usage

```bash
python -m minara.backtest --config strategy.yaml --output results/
```

Result:
```
N trades:       1,247
Win rate:       62%
Avg win:        +1.32%
Avg loss:       -1.05%
Profit factor:  1.87
Total return:   +47.3%
Max drawdown:   -18.2%
Sharpe:         2.1
```

This is what I look at before deciding to deploy.

## My pre-live checklist

Before any live deploy:

- [ ] Profit Factor > 1.5 (with fees + slippage)
- [ ] Max Drawdown < 30% of capital
- [ ] OOS performance close to IS (no overfit)
- [ ] PF > 1.2 even with ±20% param variation (robust)
- [ ] At least 100 trades (statistical significance)
- [ ] Works in both high- and low-volatility periods
- [ ] Break-even WR < actual WR - 5pp (margin of safety)

If it fails any of these, no live.

## What backtest taught me

While running, things backtest helped me discover:

1. **Add regime filter**: block entries when correlation < 0.7 → daily-loss frequency clearly down
2. **Direction asymmetry**: ETH long beats BTC long → differentiated entry thresholds
3. **DCA cap at 4**: 5+ entry WR collapses → max_entries 5 → 4 → 3
4. **Redesign TP/SL**: by R:R, not margin → break-even WR 94% → 55%
5. **no_entry_hours**: certain UTC hours had low WR → block entries

All of this was first verified in backtest, then applied live.

## Next chapter

Next: [Polymarket](https://polymarket.com/?ref=coinmage) bot — auto-trading prediction markets. A completely different market structure from Perps.
