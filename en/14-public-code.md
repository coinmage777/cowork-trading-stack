# 14. Public Code

This guide does not include the full source of the main bot. This is partly to protect alpha, and partly because copying full code into a different environment usually causes accidents instead of profits.

This chapter lists open-source resources useful for learning or starting out, along with some mini tools that may be published later.

## Open-source bots for learning

### Freqtrade
- https://github.com/freqtrade/freqtrade
- Python trading bot framework
- Backtesting + paper + live
- Wide CEX support (ccxt-based)
- Best starting point

Freqtrade's backtesting module is a useful reference when learning pair trading.

### Hummingbot
- https://github.com/hummingbot/hummingbot
- Specialized for market making and arbitrage
- Recommended for algo-trading entrants

### Jesse
- https://github.com/jesse-ai/jesse
- Python, clean API
- Strong backtesting

### CCXT
- https://github.com/ccxt/ccxt
- 100+ exchange unified library
- A common dependency on the CEX side

## SDKs / official per-exchange

### [Hyperliquid](https://miracletrade.com/?ref=coinmage)
- https://github.com/hyperliquid-dex/hyperliquid-python-sdk
- Official Python SDK
- Agent wallet system / EIP-712 signing all included

### dYdX v4
- https://github.com/dydxprotocol/v4-clients
- Python / Node clients

### Backpack
- https://github.com/backpack-exchange/backpack-api
- ed25519 signing examples

### [Polymarket](https://polymarket.com/?ref=coinmage)
- https://github.com/Polymarket/py-clob-client
- CLOB client (Python)
- The Polymarket bot referenced in this guide is built on this SDK

## Data / analysis

### Pandas + NumPy + SciPy
- Standard for backtests / stats / signal design
- Backtest code generally sits on top of these

### Vectorbt
- https://github.com/polakowo/vectorbt
- Vectorized backtesting — fast
- Strong for grid search

### Plotly / Matplotlib
- Charts / equity curves

### Jupyter / VS Code Notebook
- Essential for data exploration

## Infrastructure

### tmux
- Terminal multiplexer
- Run bots in background on VPS

### systemd
- Linux standard service manager
- Watchdog + auto-restart

### Docker
- Bot isolation / deployment — optional, suitable for those comfortable with it
- Useful for dependency conflicts

### rclone
- Cloud storage mounting
- Google Drive / S3 / OneDrive

## AI tools

### Claude (Anthropic)
- Primary tool. API + Claude Code

### Cursor
- VS Code fork, AI integrated
- https://cursor.sh

### GitHub Copilot
- Inline autocomplete

### OpenAI Codex / GPT
- ChatGPT
- Code review / algorithm design

## Mini tools that may be published

As of writing, the following tools are candidates for publication once cleaned up:

### 1) PnL Reporter
Daily / weekly PnL reports from exchange balance snapshots. A generalized version of the internal tool.

### 2) Funding Rate Monitor
Compares funding rates across N exchanges → Telegram alert when threshold gap. For monitoring before building an arb bot.

### 3) Cross-Exchange Price Aggregator
Real-time mark price across exchanges into one dashboard. WebSocket-based.

### 4) Backtest Skeleton
Mini pair-trading backtest framework. config.yaml in → metrics out. A learning version of Ch 8.

### 5) Bot Health Monitor
Separate process that monitors bot state → detects zombie / hung → Telegram alert + auto-restart.

If published, they would appear in a separate repo or as a `tools/` directory in this one.

## What will not be published (and why)

### 1) Full main pair-trading bot code
Why:
- Alpha protection
- Built around a specific environment / exchanges / capital scale, does not generalize
- Copy-running risks accidents

### 2) Exact signal weights / parameters
Why:
- Time-varying (adjusted with market shifts)
- May be overfit to the original dataset
- No guarantee they work in different setups

### 3) Specific exchanges / sizes in use
Why:
- Avoid self-trading / bot fingerprinting
- Avoid capital exposure

What this guide provides instead is **framework and mindset**. Use that to build original alpha.

## Recommended learning order

1. **freqtrade** — get familiar with the backtest / paper / live cycle
2. **ccxt** — comfort with multiple exchange APIs
3. **Code your own simple signal backtest** (z-score / RSI / momentum)
4. **Hyperliquid SDK** — first DEX bot (paper)
5. **Build your own bot** — from scratch, fitted to your environment

If you cannot write a single line of code yourself, AI can do 90% of it, but a system you do not understand cannot be operated. Anything not understood will cause an incident.

## GitHub

Mini tools and guide updates published here:
- https://github.com/coinmage777

This repo (`cowork-trading-stack`) may receive updates over time. Watch / Star to be notified.

## Next chapter

Final chapter: glossary — every trading / DeFi / bot term used in this guide, in one place.
