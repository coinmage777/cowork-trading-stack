# 13. Step-by-Step Roadmap

A path from zero to operational, organized by time / milestones. This represents one trajectory, not a universal answer. Pace accordingly.

## Before week 0

Capital: small. Starting size around $50–$100 per exchange.
Time: weekends + 1–2 evening hours weekdays.
Knowledge: basic Python + ability to read a chart.

## Week 1 — environment setup

### Goal
Tools should feel natural in hand.

### To do
- [ ] Install VS Code / Cursor
- [ ] Python 3.10+ + venv
- [ ] Git + GitHub
- [ ] Claude API key
- [ ] Create Telegram bot
- [ ] Open one exchange (Bybit or Binance recommended — stable)
- [ ] Issue API key (trading only, no withdrawal)
- [ ] Save keys in `.env` → `.gitignore` it
- [ ] Balance fetch script with ccxt

### First code
```python
# balance_check.py
import os
import asyncio
from dotenv import load_dotenv
import ccxt.async_support as ccxt

load_dotenv()

async def main():
    ex = ccxt.bybit({
        "apiKey": os.getenv("BYBIT_API_KEY"),
        "secret": os.getenv("BYBIT_SECRET"),
        "options": {"defaultType": "swap"},
    })
    balance = await ex.fetch_balance()
    print(f"USDT: {balance['USDT']['total']}")
    await ex.close()

asyncio.run(main())
```

If this works, week 1 is done.

## Week 2 — first automation

### Goal
Price monitoring + alert bot.

### To do
- [ ] Bot polling price every minute
- [ ] Telegram alert when price hits target
- [ ] Background-running (tmux / screen)

### Code
```python
import asyncio
import aiohttp
import os

async def notify(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with aiohttp.ClientSession() as s:
        await s.post(url, json={"chat_id": chat_id, "text": text})

async def price_alert(target_price: float):
    ex = ccxt.binance()
    sent = False
    while True:
        ticker = await ex.fetch_ticker("BTC/USDT")
        if not sent and ticker["last"] >= target_price:
            await notify(f"BTC hit ${target_price}")
            sent = True
        await asyncio.sleep(60)

asyncio.run(price_alert(70000))
```

## Weeks 3–4 — volume farming bot

### Goal
First trading bot. Two-sided hedge to accumulate volume only.

### To do
- [ ] Implement the simple version from Ch 5
- [ ] One exchange, one asset (BTC)
- [ ] Very small size ($10–20)
- [ ] Run for a week → analyze PnL

### Validation criteria
- Two-sided PnL sum within -1% (after fees + slippage)
- Bot runs 24 hours uninterrupted
- Telegram alerts working

## Weeks 5–8 — Obsidian + memory

### Goal
Build a knowledge base. Trade / research / content compounding.

### To do
- [ ] Install Obsidian
- [ ] Set up vault folder structure (Ch 4)
- [ ] Write daily trade journal template
- [ ] Daily journaling on trades / bot ops
- [ ] Write CLAUDE.md (project context)
- [ ] MemKraft setup (optional)
- [ ] First project research note (an exchange / token of interest)

At this point, the activity shifts from "trading" to systems thinking.

## Weeks 9–12 — pair trading, phase 1

### Goal
Backtest + paper trade.

### To do
- [ ] Collect 1 year BTC vs ETH data (1m)
- [ ] Backtest simple z-score signal (Ch 8)
- [ ] Compute metrics — Profit Factor, Win Rate, MDD
- [ ] Param sensitivity check
- [ ] Paper trade (live prices, fake balance)
- [ ] Compare paper vs backtest

### Validation criteria
- Backtest PF > 1.5
- Paper trade ±20% of backtest result

## Weeks 13–16 — pair trading live

### Goal
Small-size live + stability verification.

### To do
- [ ] Live on one exchange ([Hyperliquid](https://miracletrade.com/?ref=coinmage) is one option)
- [ ] Very small size (5% of capital)
- [ ] All trades logged to DB
- [ ] Daily PnL report (cron / daily_report.py)
- [ ] Circuit breaker (-X% daily → stop)
- [ ] 1 week run → compare with backtest

### Likely issues
- Slippage worse than backtest assumed
- Bot crash → orphan positions
- Exchange API change / rate limits
- Market regime change

This is the point where Ch 11 (operational infra) applies in full.

## Weeks 17–24 — multi-exchange / advanced

### Goal
N exchanges + signal diversification.

### To do
- [ ] Add 2–3 more exchanges (Ch 12)
- [ ] Exchange grouping (stagger entry)
- [ ] More signals — momentum / Bollinger / RSI
- [ ] Regime filter (correlation-based)
- [ ] Trailing stop
- [ ] DCA (carefully)

### Verification per change
1. Backtest
2. Paper 1 week
3. Live small 1 week
4. Stable → full size

One thing at a time.

## Weeks 25–36 — infrastructure deepening

### Goal
Systems thinking matures. More time goes to infra / monitoring than to bot code.

### To do
- [ ] VPS setup (Contabo / Hetzner)
- [ ] systemd service + watchdog
- [ ] JSON-structured logs
- [ ] Daily report auto-Telegram
- [ ] State manager + graceful restart
- [ ] Telegram commander (remote control)
- [ ] Backup automation (DB / code)
- [ ] AI memory system (CLAUDE.md + MemKraft)

At this point, the bot can run for days unattended. Time is freed.

## Week 37+ — diversify

### Goal
Strategies beyond pair trading.

### Candidates
- [Polymarket](https://polymarket.com/?ref=coinmage) / [Predict.fun](https://predict.fun/?ref=coinmage) (Ch 9)
- Cross-exchange arb (Ch 10)
- Funding arb
- New signals / new asset pairs
- Content (blog / YouTube)

Each new strategy belongs on a separate bot track. Breakage in one should not kill the others.

## End of year 1

By this point, the expected baseline:
- 1–2 bots running stably
- Auto daily PnL reports received
- New exchanges / signals added in 1–2 days
- Self-analysis of incidents and loss patterns
- Personal alpha hypotheses + a verification system

Profit magnitude varies. Year-one results often land near break-even. Real returns tend to arrive in years 2–3.

## Years 2–3 — scale

### Goal
Grow capital + new domains.

### Candidates
- Grow capital (only on verified strategies)
- New markets (prediction markets / options / DeFi yields)

A general principle: **even as profits grow, do not deploy all available capital.** Scale slowly, diversify for risk.

## Common mistakes

### 1) Going full size on day 1
Untested strategy at full size → big loss. Always 5% → 25% → 50% → 100% staircase.

### 2) Skipping backtest
"This feels right" → live → 27 days red. One hour of backtest catches it.

### 3) Leaking alpha
Bragging "my bot does this" kills the alpha. Specific details should stay private (this guide is framework only).

### 4) Too many exchanges / strategies at once
Three new exchanges in a week = debugging hell. One at a time.

### 5) Skipping monitoring
"It's running fine" → days later, big loss discovered. Five minutes a day at minimum.

### 6) Ignoring data
Decisions made by feel without checking the DB tend to focus on the wrong thing.

### 7) Zombie processes
Old instance still alive after restart → duplicate orders → big loss. Always `ps aux` confirm.

## Next chapter

Next: public code — open-source projects worth using and tools that may be published later.
