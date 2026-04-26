# 06. Multi-Perp Pair Trading

A core strategy for the main bot. Across multiple Perp DEXs, BTC/ETH-style pairs run simultaneously long-short, extracting alpha from the relative movement of two correlated assets.

## What is pair trading

Idea:
- Two assets A and B usually move together (high correlation)
- Their price ratio / spread occasionally drifts from the mean (z-score 1.5+)
- → If A is overpriced vs B: short A + long B
- → On mean reversion, both legs profit

In crypto the most common pair is **BTC vs ETH**. Both have similar market beta and typical correlation > 0.8.

### Why pair trading beats directional

- **Hedged market exposure** — if BTC drops 10%, both legs drop together, PnL impact is small
- **Stable use of leverage** — 10x directional is risky for liquidation, but a hedged pair at 10x is much safer
- **Backtestable** — design signals from the time series of the price ratio between two assets

### Downsides

- Complex — twice the variables of a single-side trade
- Cross-exchange operation doubles capital, API, monitoring needs
- Sudden one-asset moves (an "ETH alpha unwind") cause sharp losses

## Bot structure — overview

The multi-Perp pair trading bot (built on top of the `mpdex` framework):

```
config.yaml          → exchanges / strategy / sizing
multi_runner.py      → main loop, manages per-exchange pair trader instances
strategies/
  pair_trader.py     → single-exchange pair trading logic
  nado_pair_scalper.py → fast momentum + spread variant
  strategy_evolver.py  → auto-tunes signal weights
mpdex/               → exchange adapters (Hyperliquid, GRVT, Lighter, ...)
state_manager.py     → bot state persistence (for DCA / trailing recovery)
db.py                → SQLite trade / equity logging
```

### Factory pattern

Adding a new exchange must be cheap. Factory + lazy loading:

```python
# factory.py
def create_exchange(name: str, key_params: dict):
    if name == "hyperliquid":
        from mpdex.hyperliquid import HyperliquidExchange
        return HyperliquidExchange(**key_params)
    elif name == "grvt":
        from mpdex.grvt import GrvtExchange
        return GrvtExchange(**key_params)
    # ...
```

`config.yaml` just names the exchange; everything else loads automatically.

### Unified interface

Every exchange exposes the same methods:

```python
class BaseExchange:
    async def get_mark_price(self, symbol: str) -> Decimal: ...
    async def get_position(self, symbol: str) -> Position: ...
    async def create_order(self, symbol: str, side: str, qty: Decimal, 
                           order_type: str = "limit", price: Decimal = None) -> Order: ...
    async def cancel_orders(self, symbol: str): ...
    async def close_position(self, symbol: str): ...
    async def get_collateral(self) -> Decimal: ...
```

The bot core works against this interface, exchange-agnostic.

## Signals — entry / exit rules

### 1) Momentum diff

Basic:
- Coin1 (BTC) 14-minute return vs Coin2 (ETH) 14-minute return
- If the gap exceeds threshold (e.g., 2%), enter
  - BTC > ETH: BTC short + ETH long (anti-momentum bet)

Rationale: short-term momentum mean-reverts strongly. The pair version is more stable than directional.

### 2) Spread z-score

```python
def calc_spread_zscore(prices_btc, prices_eth, lookback=60):
    spread = np.log(prices_btc) - np.log(prices_eth)
    mean = spread[-lookback:].mean()
    std = spread[-lookback:].std()
    z = (spread[-1] - mean) / std
    return z
```

- |z| ≥ 1.5: enter (direction follows sign of z)
- |z| < 0.3: exit (convergence)

### 3) Bollinger breakout / RSI divergence (auxiliary)

The two signals above serve as primary. Auxiliaries:
- Bollinger: spread outside 2σ
- RSI divergence: BTC RSI 70+ paired with ETH RSI 50- etc.

Auxiliary weights kept small (0.10–0.20).

### 4) Regime filter — critical

Pair trading fails when correlation is low (assets moving independently breaks the mean-reversion premise).

```python
def regime_ok(prices_btc, prices_eth, window=24):
    # 24-candle (6-hour) Pearson correlation
    corr = np.corrcoef(prices_btc[-window:], prices_eth[-window:])[0, 1]
    return corr >= 0.7  # block entries below 0.7
```

Adding this filter visibly reduces daily loss frequency.

### 5) Direction asymmetry — data-driven

Observed data: ETH long (coin2_long) WR 80% vs BTC long (coin1_long) WR 70%.

Probable cause: alt-season bias / beta differential. Whatever the cause, the data should be followed.

```python
COIN2_LONG_ENTRY_BONUS = 0.15  # 15% threshold discount for ETH long entries
```

## Exit priority

The order in which the bot evaluates exit conditions (top to bottom):

1. **Hard stop** (-2.5% PnL): immediate close, no exceptions
2. **Trailing stop**: once activated, callback hit
3. **Fixed take-profit** (+2% PnL): used to be 0.4%, R:R math forced 5x
4. **Momentum loss cap**: disabled (hard_stop already covers this)
5. **Momentum exit / spread convergence**: signal-based exit
6. **DCA**: if conditions met, scale in

### R:R math

This is a lesson learned the expensive way. **TP/SL ratio determines break-even win rate.**

Example: TP 0.4% + SL 3% → break-even WR = 3 / (0.4 + 3) = 88%. Such a rate is not sustainable.

A common mistake: running TP 0.4% / SL 3% → 27 consecutive losing days.

After fix: TP 2% / SL 2.5% → break-even WR 55%. Actual WR 67–84% → profitable.

**TP/SL must be designed by R:R, not margin percentages.** Verify the math before going live.

### Trailing stop

Standard:
- Activation: PnL +1.5%
- Callback: 1.0%
- If profits get larger (+3%): tighten callback to 0.5% (lock in gains)

```python
class TrailingStop:
    def __init__(self, activation_pct, callback_pct, tighten_above, tighten_callback):
        self.activated = False
        self.peak = 0
        # ...

    def update(self, current_pnl_pct):
        if not self.activated and current_pnl_pct >= self.activation_pct:
            self.activated = True
            self.peak = current_pnl_pct
        if self.activated:
            self.peak = max(self.peak, current_pnl_pct)
            cb = self.tighten_callback if self.peak >= self.tighten_above else self.callback_pct
            if self.peak - current_pnl_pct >= cb:
                return True  # close
        return False
```

## DCA (Dollar Cost Averaging) — scaling in

Adding to a position when entry timing was off, lowering the average. **Dangerous; powerful when used right.**

Rules:
- Max entries: 3
- Add condition: when price has worsened by N% from previous entry (spread basis)
- Each tranche: same size (no martingale)
- Track WR by DCA depth: if 4+ entry WR drops sharply, lower max_entries

Observed data:
- Entries 1–3: solid WR (70%+)
- 4+: drops (48%)
- 9–10: concentrated losses

→ cap max_entries at 3 or 5.

## Exchange grouping (stagger)

Same problem as in volume farming — entering all exchanges simultaneously creates market impact against the position. So:

| Group | Delay | Momentum mult | Effective threshold | Exchanges |
|-------|-------|---------------|---------------------|-----------|
| GA | 0s | 0.8x | 1.6% | 6 |
| GB | 30s | 1.0x | 2.0% | 6 |
| GC | 60s | 1.5x | 3.0% | 6 |
| GD | 90s | 2.0x | 4.0% | 4 |

Later groups demand stronger signals → reduces simultaneous exits.

## Strategy Evolver

A component that auto-tunes signal weights. Every 6 hours:
1. Analyze recent trade performance
2. Compute per-signal contribution
3. Adjust weights (better signals get heavier)
4. Restart next cycle with new weights

**Anti-overfitting floor**:
```yaml
min_signal_weights:
  momentum_diff: 0.15
  spread_zscore: 0.10
```

Without these floors, the evolver can drive core signals to zero and the bot becomes incoherent.

## Operational tips

### 1) Start small
Pair trading bots have many variables — entry signal, exit signal, sizing, leverage, exchange, pair, market volatility. Start with small capital, one exchange, one pair. Expand only after stability.

### 2) Data-driven decisions
"Bump SL by feel" is forbidden. Look at the actual loss distribution in DB. Before any change, analyze N trades.

### 3) Change one thing at a time
SL / TP / size / leverage all changed at once → no way to attribute effects. A/B style: one at a time.

### 4) Circuit breaker mandatory
Auto-stop on daily -X% loss. Defaults sit between -$30 and -$150 depending on capital scale.

### 5) Telegram integration
Every entry / exit / error to Telegram. Bot status is reachable from mobile.

## Current setup (snapshot)

Active exchanges: 17+
Main pairs: BTC/ETH, ETH/SOL
Leverage: 10x (was 15x)
Size: $50 margin per entry
Max concurrent per exchange: 3
Daily PnL distribution: ±2–3% of capital

This does not translate to a specific number. Some days are red, some green. If the average is positive over time, the system works. The truth is checked daily via balance tracker (DB PnL can be inaccurate — covered later).

## Next chapter

Next: kimchi premium and cross-venue arb. How to automate the price gap between Korean and global exchanges.
