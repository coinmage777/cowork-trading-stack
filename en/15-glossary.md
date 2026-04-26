# 15. Glossary

Every term used in this guide, alphabetical. For quick lookup.

## A

**Adverse Selection** — In a market, your fills tend to come when the counterparty has informational advantage. For bots, the fill itself can be a negative signal.

**Agent Wallet** — A separate key delegated trading rights by a main wallet. On DEXs like Hyperliquid, lets bots trade without exposing the main key.

**Aiohttp** — Python async HTTP library. Standard for async exchange API calls.

**API Key / Secret** — Exchange API credentials. Trading-only, never withdrawal.

**APR / APY** — Annual Percentage Rate / Yield. For e.g. funding-arb annualized returns.

**Arbitrage** — Same asset trading at different prices on different venues; capture by simultaneous opposite trades.

**ATR (Average True Range)** — Volatility measure. Used in dynamic sizing / stop-loss width.

## B

**Backtest** — Simulating a strategy on historical data to measure performance. Mandatory before live.

**Basis** — Spot–perp price difference. Basis arb is a form of carry trade.

**BBO (Best Bid/Offer)** — Highest bid / lowest ask in the order book. Reference for limit-order entry.

**Bollinger Bands** — Standard-deviation-based price bands. Auxiliary signal for breakout / reversion.

**Builder Code** — Hyperliquid's routing mechanism. Per-front-end (Miracle, DreamCash, etc.) codes for fee distribution.

## C

**ccxt** — 100+ exchange unified library (Python / JS). Standard for CEX bots.

**CEX** — Centralized Exchange. Binance, OKX, Bybit, etc.

**Circuit Breaker** — Auto-stop on daily -X% loss. Prevents unbounded losses.

**Clearinghouse State** — Hyperliquid-style DEX user balance / position state.

**CLOB (Central Limit Order Book)** — Central order book matching. Polymarket uses this.

**Cloid** — Client Order ID. Exchange order identifier. Front-ends like Miracle require a specific cloid prefix.

**Correlation** — Synchronicity between two assets' prices. Used as a regime filter in pair trading.

**Cross-Venue** — Across exchanges. Cross-venue arb = arb of inter-exchange price differences.

## D

**DCA (Dollar Cost Averaging)** — Adding to a position to lower the average. Two-sided risk — powerful when used right, ruinous when not.

**DEX** — Decentralized Exchange. Hyperliquid, dYdX, GMX, Lighter etc.

**Drawdown** — Loss from capital high. Max Drawdown is a core evaluation metric.

## E

**EIP-712** — Ethereum standard for typed-data signing. Used by Hyperliquid, Reya, etc.

**Equity Tracker** — Periodically snapshots per-exchange balance. Source of truth for real PnL.

**Entry / Exit** — The two sides of signal design.

## F

**Factory Pattern** — Object creation abstraction. Exchange adapter registration / loading pattern.

**Funding Rate** — Perpetual exchange's price/index alignment mechanism. Usually paid every 8h.

**Funding Arbitrage** — Capturing funding-rate differences across exchanges or assets.

## G

**Gas Fee** — On-chain transaction cost. EVM chains use ETH / BNB / MATIC etc.

**Ghost Position** — Position present on the exchange but not in your DB. Major cause of losses.

**Graceful Restart** — Restart while preserving positions / state. Requires a state manager.

## H

**Hedging** — Reducing exposure by holding offsetting positions. The core of pair trading.

**HMAC** — Hash-based Message Authentication Code. Most CEXs sign requests this way.

**Hot Reload** — Refresh config without restart. SIGHUP or file trigger.

**Hyperliquid (HL)** — My main DEX. Self-L1, EIP-712 signing, builder code system.

## I

**Iceberg Order** — Splitting a big order into small visible chunks. Common in market making.

**IOC (Immediate-or-Cancel)** — Auto-cancels if not matched. Useful for one-side-only protection in arb.

**IP Whitelist** — Restrict API key to specific IP. Security basics.

## K

**Kelly Criterion** — Optimal bet size formula given edge. Full Kelly is too aggressive in practice; quarter Kelly is reasonable.

**Kimchi Premium** — Korean exchange prices > global on USD-equivalent basis. Caused by capital flow asymmetry.

## L

**Latency** — API call / response time. Critical in arb bots.

**Leverage** — Position size relative to capital. My pair trading defaults to ~10x.

**Limit Order** — Buy / sell at a specific price. Maker = adds liquidity, Taker = removes.

**Liquidation** — Forced position close due to margin shortfall. Always a consideration with leverage.

**Look-ahead Bias** — Backtest leak where future data informs past decisions.

## M

**Maker / Taker** — Provider vs consumer of order book liquidity. Different fees.

**Margin** — Collateral for a position. Leverage = position / margin.

**Mark Price** — Exchange's reference / liquidation price. Usually spot index + funding.

**Market Order** — Immediate match-price order. Slippage applies.

**MemKraft** — The AI memory system I use, integrated with Obsidian.

**Momentum** — Short-term price change. One of pair trading's entry signals.

## N

**Notional Value** — Position's nominal value. Size × price.

## O

**OHLCV** — Open / High / Low / Close / Volume. Standard candle data.

**Orphan Position** — Position living on the exchange separately from your bot's state. Similar to ghost.

**Out-of-sample (OOS)** — Data unused during model training. Used in walk-forward.

## P

**Paper Trade** — Live prices + fake balance simulation. Pre-live verification stage.

**Pair Trading** — Mean-reversion bet on the price ratio / spread between two correlated assets.

**Perp (Perpetual Futures)** — Futures contract with no expiry. Uses funding to align with index.

**PnL (Profit and Loss)** — Realized vs unrealized.

**Polymarket** — Polygon-based prediction market. Binary outcomes.

**Position** — Active position (long / short / flat).

**Post-Only** — Maker-only fill. Auto-cancels if it would be a taker.

**Profit Factor** — Gross profit / gross loss. > 1.5 is the threshold for meaningful alpha.

## R

**R:R (Risk-Reward)** — Stop-loss vs take-profit ratio. Determines break-even win rate.

**Rate Limit** — API call frequency cap. Differs per exchange.

**Regime Filter** — Entry condition based on market state (correlation / volatility / etc.). Core safety in pair trading.

**REST API** — HTTP-based. For polling.

**Rolling Correlation** — Correlation over a moving window. Tracks change over time.

**RSI (Relative Strength Index)** — Momentum oscillator. 70+ overbought, 30- oversold.

## S

**Sharpe Ratio** — Return / volatility. 1.5+ is solid alpha.

**Signal** — Basis for entry / exit decisions. z-score, momentum, etc.

**Slippage** — Difference between intended and executed price.

**SDK (Software Development Kit)** — Exchange-provided client library.

**Spread** — Bid–ask difference, or two-asset price difference.

**State Manager** — Persists bot state. Required for graceful restart.

**Strategy Evolver** — Auto-tunes signal weights, every 6 hours, based on performance.

**StarkNet** — Layer 2. Used by EdgeX, Paradex, etc.

## T

**Take Profit (TP) / Stop Loss (SL)** — Exit prices for profit / loss. Designed by R:R.

**Telegram Bot** — Alert / remote-control interface. Created via BotFather.

**Tick** — Smallest price unit. Differs per exchange / asset.

**Trailing Stop** — Stop level follows favorable price moves. Locks in gains.

**Trigger File** — Windows-friendly bot control pattern. File creation → bot detects → handles.

## V

**Venv (Virtual Environment)** — Python dependency isolation. Mandatory for SDK conflict avoidance.

**Volume Farming** — Accumulating volume-based points / rewards. Two-sided hedge for zero price exposure.

## W

**Walk-forward** — Time-ordered train/test repetition. Validates backtest robustness.

**WAL (Write-Ahead Log)** — SQLite concurrency mode. Required when multiple processes write to the same DB.

**WebSocket (WS)** — Real-time bidirectional comms. For market data and fills.

**Win Rate** — Fraction of profitable trades. Break-even WR = SL / (TP + SL).

## Z

**z-score** — How many standard deviations from the mean. Core signal in pair trading.

---

## Closing

This guide is a snapshot of the system I run today. Over time exchanges will close, new ones will appear, signals will stop working, and new ones will be discovered. That's the nature of the field.

**Half of this guide will be outdated in six months.** The framework — the automation mindset, data-driven decisions, infra / operational principles, AI integration — those don't expire.

If this guide helped someone start their first piece of automation, that's enough.

Returns come from systems. Systems come from time. Time comes from patience.

Wishing you good alpha.
