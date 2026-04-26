# 05. Volume Farmer

Before any serious trading bot, the simplest piece of automation is the volume farmer. It is useful when an exchange runs a points / rewards season based on trade volume.

## What is a volume farmer

The basic idea: **enter and exit both sides simultaneously, accumulating volume while keeping price exposure near zero.**

Example: BTC long $1000 + BTC short $1000 → price moves but PnL ≈ 0, while $2000 of volume gets logged.

Running this every N minutes accumulates points by season's end.

### Where it works

- **Perp DEXs during airdrop seasons** ([Hyperliquid](https://miracletrade.com/?ref=coinmage), [Lighter](https://app.lighter.xyz/?referral=GMYPZWQK69X4), [EdgeX](https://pro.edgex.exchange/referral/570254647), Nado, [GRVT](https://grvt.io/exchange/sign-up?ref=1O9U2GG), etc.)
- **Volume-based exchange campaigns** (Bybit, OKX run these often)
- **Maker-rebate exchanges** — if maker fees are negative, the volume itself is profit

### Where it doesn't

- Expensive exchanges (CEX 0.05% taker fee → 0.1% loss per cycle)
- Campaigns with "trade PnL > 0" requirements
- Exchanges that detect / ban same-account self-matching

## Structure — simplest version

```python
import asyncio
import ccxt.async_support as ccxt

async def volume_farm(exchange, symbol, size_usd, sleep_sec):
    while True:
        try:
            ticker = await exchange.fetch_ticker(symbol)
            price = ticker["last"]
            qty = size_usd / price

            # 1. open long (market)
            long_order = await exchange.create_market_buy_order(symbol, qty)
            await asyncio.sleep(1)

            # 2. open short (market) — same size
            short_order = await exchange.create_market_sell_order(symbol, qty)
            await asyncio.sleep(1)

            # 3. close both
            await exchange.create_market_sell_order(symbol, qty)  # close long
            await asyncio.sleep(1)
            await exchange.create_market_buy_order(symbol, qty)   # close short

            print(f"[{symbol}] cycle done, vol ~${size_usd*4}")
        except Exception as e:
            print(f"error: {e}")
        await asyncio.sleep(sleep_sec)

async def main():
    ex = ccxt.bybit({
        "apiKey": os.getenv("BYBIT_API_KEY"),
        "secret": os.getenv("BYBIT_SECRET"),
        "options": {"defaultType": "swap"},
    })
    await volume_farm(ex, "BTC/USDT:USDT", size_usd=100, sleep_sec=300)

asyncio.run(main())
```

That gives roughly $1200 volume per hour, ~$30k per day. Modest capital is sufficient.

## Why the above breaks in production

The naive version is illustrative, not production-ready. Common issues encountered in practice:

### 1) Slippage means PnL never converges to zero

Two market orders incur ~0.02–0.05% slippage on each side. Small losses accumulate per cycle.

**Fix**: replace market orders with BBO (best bid/offer) limit orders, or post-only.

```python
ob = await exchange.fetch_order_book(symbol)
best_bid = ob["bids"][0][0]
best_ask = ob["asks"][0][0]
# long: limit buy at best_bid (maker)
# short: limit sell at best_ask (maker)
```

Only maker fees apply, and on some exchanges, rebates are received.

### 2) One-sided fills create exposure

Market orders fill almost instantly, but limits may not. If only one side fills, the position becomes directional.

**Fix**:
- After one side fills, if the other doesn't fill within N seconds, cancel and retry
- Or accept some slippage with market orders for robustness

```python
async def safe_pair_open(exchange, symbol, qty, timeout=10):
    long_order = await place_post_only(exchange, symbol, "buy", qty)
    short_order = await place_post_only(exchange, symbol, "sell", qty)
    
    deadline = time.time() + timeout
    while time.time() < deadline:
        long_filled = await is_filled(exchange, long_order["id"])
        short_filled = await is_filled(exchange, short_order["id"])
        if long_filled and short_filled:
            return True
        await asyncio.sleep(0.5)
    
    # neither filled or only one filled
    await exchange.cancel_order(long_order["id"], symbol)
    await exchange.cancel_order(short_order["id"], symbol)
    
    # if one side filled, market-close the residual exposure
    return False
```

### 3) Exchange ban risk

Some exchanges detect same-account self-matching. Sending two opposite orders at exactly the same price simultaneously raises flags.

**Fix**:
- Slightly different prices (best_bid + 1 tick / best_ask - 1 tick)
- Time gap (long → wait 30s → short)
- Slightly different sizes (allow 10% jitter)
- Two accounts (one long-only, one short-only — safest)

### 4) Volatile markets cause two-sided losses

If price moves 1% from entry to exit, both sides combined may not zero out (because of fees and funding).

**Fix**:
- Avoid volatile windows (CPI, FOMC, large listings)
- Keep holding time short (under 5 minutes)
- Only enter when funding is low

### 5) Funding

Perps have funding every 8 hours. While both legs are held, long pays + short receives = 0. During the brief moment one leg is closed, however, funding cost is incurred.

**Fix**: avoid the 00, 08, 16 UTC funding windows.

## Established pattern

After repeated iteration, the following pattern has proven reliable:

### Per-exchange modularization

An adapter layer hides exchange differences:

```python
class ExchangeAdapter:
    async def place_post_only(self, symbol, side, qty, price): ...
    async def cancel_order(self, order_id, symbol): ...
    async def get_position(self, symbol): ...
    async def get_balance(self): ...
    # ...
```

Each exchange (`hyperliquid.py`, `bybit.py`, ...) implements this. The bot core only calls the adapter.

### Cycle design

1. **Open**: BBO limit, both legs (1–5 second offset)
2. **Confirm fills**: 10-second timeout, fall back to market on failure
3. **Hold**: 30s–5min (randomized — anti-detection)
4. **Close**: same pattern
5. **Wait until next cycle**: random 60–300s

### Monitoring

- Per-cycle PnL logged (DB)
- Cumulative volume / cumulative fees
- Daily efficiency: `points / cost`
- Failure rate: % cycles with one-sided fill

### Choosing exchanges

Metrics worth tracking:

| Metric | Good value |
|--------|------------|
| Maker fee | < 0.005% (or negative) |
| Taker fee | < 0.05% |
| Funding volatility | Stable (< 0.05% in any hour) |
| Order book depth | Absorbs your size cleanly |
| API stability | < 1% 5xx errors over 24h |
| Campaign ROI | Points value per dollar of volume |

### Capital efficiency

$100k of capital is not required to push $100k of volume. With $1k size per cycle and 100 cycles, that's $100k volume on $1k capital.

Leverage shrinks it further. 5x leverage means $200 capital can support $1k size. Liquidation risk applies — manage it.

## Exchange grouping

Running multiple exchanges in parallel is common. If they all enter at the same instant, market impact is large and detection risk grows. Staggering helps:

| Group | Stagger | Size mult | Exchanges |
|-------|---------|-----------|-----------|
| GA | 0s | 1.0x | 6 exchanges |
| GB | 30s | 1.0x | 6 exchanges |
| GC | 60s | 1.5x | 6 exchanges |
| GD | 90s | 2.0x | 4 exchanges |

This distributes market impact and reduces algorithmic detection risk.

## Next chapter

Next: real trading bot territory — multi Perp DEX pair trading. The core strategy of the main bot.
