# 14. Public Code

This guide doesn't include the full source of my main bot. Partly to protect alpha, partly because copying full code into a different environment usually causes accidents instead of profits.

This chapter lists open-source resources useful for learning / starting, and some mini tools I might publish later.

## Open-source bots for learning

### Freqtrade
- https://github.com/freqtrade/freqtrade
- Python trading bot framework
- Backtesting + paper + live
- Wide CEX support (ccxt-based)
- Best starting point

I referenced freqtrade's backtesting module while learning pair trading.

### Hummingbot
- https://github.com/hummingbot/hummingbot
- Specialized for market making and arbitrage
- Strong recommendation for algo-trading entrants

### Jesse
- https://github.com/jesse-ai/jesse
- Python, clean API
- Strong backtesting

### CCXT
- https://github.com/ccxt/ccxt
- 100+ exchange unified library
- I rely on ccxt for the CEX side

## SDKs / official per-exchange

### Hyperliquid
- https://github.com/hyperliquid-dex/hyperliquid-python-sdk
- Official Python SDK
- Agent wallet system / EIP-712 signing all included

### dYdX v4
- https://github.com/dydxprotocol/v4-clients
- Python / Node clients

### Backpack
- https://github.com/backpack-exchange/backpack-api
- ed25519 signing examples

### Polymarket
- https://github.com/Polymarket/py-clob-client
- CLOB client (Python)
- My Polymarket bot is built on this SDK

## Data / analysis

### Pandas + NumPy + SciPy
- Standard for backtests / stats / signal design
- All my backtest code sits on top of these

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
- Bot isolation / deployment — I don't, but if you're comfortable, fine
- Useful for dependency conflicts

### rclone
- Cloud storage mounting
- Google Drive / S3 / OneDrive

## AI tools

### Claude (Anthropic)
- My main. API + Claude Code

### Cursor
- VS Code fork, AI integrated
- https://cursor.sh

### GitHub Copilot
- Inline autocomplete

### OpenAI Codex / GPT
- ChatGPT
- Code review / algorithm design

## Mini tools I might publish

As of writing, these are tools I'd consider publishing once cleaned up:

### 1) PnL Reporter
Daily / weekly PnL reports from exchange balance snapshots. A generalized version of what I run.

### 2) Funding Rate Monitor
Compares funding rates across N exchanges → Telegram alert when threshold gap. For monitoring before building an arb bot.

### 3) Cross-Exchange Price Aggregator
Real-time mark price across exchanges into one dashboard. WebSocket-based.

### 4) Backtest Skeleton
Mini pair-trading backtest framework. config.yaml in → metrics out. A learning version of Ch 8.

### 5) Bot Health Monitor
Separate process that monitors bot state → detects zombie / hung → Telegram alert + auto-restart.

If published, they'd appear in a separate repo or as a `tools/` directory in this one.

## What I won't publish (and why)

### 1) Full main pair-trading bot code
Why:
- Alpha protection
- Built around my environment / exchanges / capital scale, doesn't generalize
- Copy-running risks accidents

### 2) Exact signal weights / parameters
Why:
- Time-varying (adjusted with market shifts)
- May be overfit to my data
- No guarantee they work in different setups

### 3) Specific exchanges / sizes I run
Why:
- Avoid self-trading / bot fingerprinting
- Avoid capital exposure

What this guide gives instead is **framework and mindset**. Use that to build your own alpha.

## Recommended learning order

1. **freqtrade** — get familiar with the backtest / paper / live cycle
2. **ccxt** — comfort with multiple exchange APIs
3. **Code your own simple signal backtest** (z-score / RSI / momentum)
4. **Hyperliquid SDK** — first DEX bot (paper)
5. **Build your own bot** — from scratch, fitted to your environment

If you can't write a single line of code yourself, AI can do 90% of it, but you can't operate what you don't understand. Anything you don't understand will cause an incident.

## My GitHub

Mini tools and guide updates I publish:
- https://github.com/coinmage777

This repo (`cryptomage-trading-guide`) may receive updates over time. Watch / Star to be notified.

## Next chapter

Final chapter: glossary — every trading / DeFi / bot term used in this guide, in one place.
